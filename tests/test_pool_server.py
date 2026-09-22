"""The pool server's promises, tested against the real HTTP server.

Everything here is about the parts that must not be broken: a key that never
leaves the box, a stranger who cannot spend the budget, and a rate-limited
account that gets left alone instead of retried.
"""
import json
import sqlite3
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

SERVER = Path(__file__).resolve().parent.parent / "server"
# The server is the operator's own box — it holds the keys — so it is not part of
# the published repository. These tests run wherever that file exists.
pytestmark = pytest.mark.skipif(not (SERVER / "pool_server.py").exists(),
                                reason="pool server is not in this checkout")
sys.path.insert(0, str(SERVER))
import pool_server  # noqa: E402

KEYS = {"groq": ["gsk_first_secret", "gsk_second_secret"]}


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    """A live server on a random port, with the upstream replaced by a recorder."""
    pool_server._state["db"] = pool_server.open_db(str(tmp_path / "pool.db"))
    pool_server._state["keys"] = {k: list(v) for k, v in KEYS.items()}
    pool_server.sync_keys()
    monkeypatch.setenv("BEECODE_POOL_ADMIN", "admin-secret")
    calls = []
    answers = [(200, json.dumps({"choices": [{"message": {"content": "жужж"}}],
                                 "usage": {"total_tokens": 40}}))]

    def fake_upstream(provider, api_key, body, begin=None, on_chunk=None):
        calls.append({"provider": provider, "key": api_key, "body": body})
        status, text = answers[min(len(calls) - 1, len(answers) - 1)]
        if begin:
            begin(status)
        return status, text

    monkeypatch.setattr(pool_server, "call_upstream", fake_upstream)
    server = ThreadingHTTPServer(("127.0.0.1", 0), pool_server.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield type("Pool", (), {"base": base, "calls": calls, "answers": answers,
                            "db": pool_server._state["db"]})()
    server.shutdown()


def enroll(client, base, ip=None):
    headers = {"X-Forwarded-For": ip} if ip else {}
    response = client.post(base + "/v1/enroll", json={}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["token"]


def complete(client, base, token, provider="groq", **body):
    payload = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "привет"}],
               "pool_provider": provider}
    payload.update(body)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(base + "/v1/chat/completions", json=payload, headers=headers)


def test_health_says_alive_and_nothing_about_capacity(pool):
    """Which upstreams are loaded and how many accounts sit behind them is a plan."""
    with httpx.Client() as client:
        body = client.get(pool.base + "/healthz").json()
    assert body == {"ok": True}


def test_a_completions_request_needs_a_seat(pool):
    with httpx.Client() as client:
        assert complete(client, pool.base, "").status_code == 401
        assert complete(client, pool.base, "made-up-token").status_code == 401
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token)
        assert answer.status_code == 200
        assert answer.json()["choices"][0]["message"]["content"] == "жужж"


def test_the_key_never_leaves_the_server_at_all(pool):
    """Not even its tail: that was a stable id for one account across every seat,
    which let a stranger aim at an account and measure the pool through seats."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        body = complete(client, pool.base, token).json()
    assert "pool_key" not in body
    assert "cret" not in json.dumps(body)
    assert body["pool_lane"].startswith("groq-")
    assert pool.calls[0]["key"] == "gsk_first_secret", "the upstream really got the key"


def test_a_rate_limited_key_is_cooled_down_and_the_next_one_tried(pool):
    pool.answers[:] = [(429, '{"error":"rate limit"}'),
                       (200, json.dumps({"choices": [{"message": {"content": "со второго"}}]}))]
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token)
    assert answer.status_code == 200
    assert answer.json()["choices"][0]["message"]["content"] == "со второго"
    assert [c["key"] for c in pool.calls] == ["gsk_first_secret", "gsk_second_secret"]
    row = pool.db.execute("SELECT cooldown_until, fails FROM keys WHERE api_key=?",
                          ("gsk_first_secret",)).fetchone()
    assert row["fails"] == 1 and row["cooldown_until"] > 0


def test_when_every_key_is_cooling_the_answer_is_429_not_a_hang(pool, monkeypatch):
    import time as clock
    pool.db.execute("UPDATE keys SET cooldown_until=?", (clock.time() + 600,))
    pool.db.commit()
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token)
    assert answer.status_code == 429
    assert int(answer.headers.get("Retry-After", 0)) > 0


def test_the_daily_budget_is_enforced_per_seat(pool):
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        pool.db.execute("UPDATE tokens SET requests=?, requests_limit=? WHERE token=?",
                        (5, 5, token))
        pool.db.commit()
        answer = complete(client, pool.base, token)
        assert answer.status_code == 429
        assert "budget" in answer.json()["error"]
        # A second seat is not punished for the first one's spending.
        other = enroll(client, pool.base)
        assert complete(client, pool.base, other).status_code == 200


def test_a_forged_forwarded_header_does_not_mint_seats(pool):
    """The whole per-IP limit used to rest on a header the caller writes.

    Nothing here spoofs an address: every request arrives from 127.0.0.1, and
    without a declared trusted proxy that is what the pool counts.
    """
    with httpx.Client() as client:
        for index in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip=f"8.8.8.{index}")
        blocked = client.post(pool.base + "/v1/enroll", json={},
                              headers={"X-Forwarded-For": "8.8.8.99"})
        assert blocked.status_code == 429


def test_a_declared_proxy_is_the_only_one_whose_header_is_believed(pool, monkeypatch):
    """With a trusted proxy in front, the counted address is the client's — so one
    client keeps its limit and the building next to it is not punished for it."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.1")
    with httpx.Client() as client:
        for _ in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip="203.0.113.7")
        assert client.post(pool.base + "/v1/enroll", json={},
                           headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 429
        assert client.post(pool.base + "/v1/enroll", json={},
                           headers={"X-Forwarded-For": "198.51.100.3"}).status_code == 200


def test_a_client_cannot_point_the_pool_at_an_arbitrary_url(pool):
    """The model decides which account answers; a provider named by the client is
    only honoured when it is one of the fixed upstreams, and never a URL."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, provider="http://169.254.169.254/")
        assert answer.status_code == 200, "the model still resolved to a real provider"
        assert pool.calls[-1]["provider"] == "groq"
        assert pool.calls[-1]["key"] == "gsk_first_secret"


def test_a_prompt_bigger_than_the_pool_accepts_is_refused_before_parsing(pool):
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        big = {"model": "m", "pool_provider": "groq",
               "messages": [{"role": "user", "content": "x" * (pool_server.MAX_BODY + 10)}]}
        response = client.post(pool.base + "/v1/chat/completions", json=big,
                               headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 413


def test_admin_endpoints_need_the_admin_token(pool):
    with httpx.Client() as client:
        assert client.get(pool.base + "/v1/pool").status_code == 403
        report = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secret"})
        assert report.status_code == 200
        assert "gsk_first_secret" not in report.text
        token = enroll(client, pool.base)
        revoked = client.post(pool.base + "/v1/admin/revoke", json={"token": token},
                              headers={"X-Admin": "admin-secret"})
        assert revoked.status_code == 200
        assert complete(client, pool.base, token).status_code == 401, "a revoked seat is gone"


def test_a_seat_awaiting_approval_is_not_served(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_APPROVAL", "1")
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        assert complete(client, pool.base, token).status_code == 403
        client.post(pool.base + "/v1/admin/approve", json={"token": token},
                    headers={"X-Admin": "admin-secret"})
        assert complete(client, pool.base, token).status_code == 200


def test_the_budget_rolls_over_when_the_day_changes(pool):
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        pool.db.execute("UPDATE tokens SET day=?, requests=? WHERE token=?",
                        ("2000-01-01", 999, token))
        pool.db.commit()
        assert complete(client, pool.base, token).status_code == 200
        row = pool.db.execute("SELECT requests, day FROM tokens WHERE token=?", (token,)).fetchone()
        assert row["day"] == pool_server.today() and row["requests"] == 1


# --- the client side of the same wire ---------------------------------------

def test_the_provider_talks_to_the_pool_and_gets_the_answer(pool):
    import asyncio

    from beeagent.providers.pool import PoolProvider, enroll

    token = enroll(pool.base)["token"]
    provider = PoolProvider(url=pool.base, token=token)
    answer = asyncio.run(provider.chat([{"role": "user", "content": "привет"}], "llama-3.3-70b"))
    assert answer == "жужж"
    assert pool.calls[-1]["key"] == "gsk_first_secret"


def test_the_provider_keeps_thinking_apart_from_the_answer(pool, monkeypatch):
    import asyncio

    from beeagent.providers.pool import PoolProvider, enroll

    def streaming(provider_name, api_key, body, begin=None, on_chunk=None):
        if begin:
            begin(200)
        if on_chunk:
            on_chunk('data: ' + json.dumps({"choices": [{"delta": {"reasoning_content": "думаю"}}]}))
            on_chunk('data: ' + json.dumps({"choices": [{"delta": {"content": "раз"}}]}))
            on_chunk("data: [DONE]")
        return 200, ""

    monkeypatch.setattr(pool_server, "call_upstream", streaming)
    provider = PoolProvider(url=pool.base, token=enroll(pool.base)["token"])

    async def collect():
        return [pair async for pair in provider.chat_stream([{"role": "user", "content": "х"}], "m")]

    assert asyncio.run(collect()) == [("reasoning", "думаю"), ("content", "раз")]


def test_a_client_without_a_seat_is_told_to_enroll(pool):
    import asyncio

    from beeagent.providers.pool import PoolProvider

    provider = PoolProvider(url=pool.base, token="")
    with pytest.raises(Exception) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert "/pool enroll" in str(raised.value)
