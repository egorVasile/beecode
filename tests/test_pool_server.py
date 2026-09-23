"""The pool server's promises, tested against the real HTTP server.

Everything here is about the parts that must not be broken: a key that never
leaves the box, a stranger who cannot spend the budget, and a rate-limited
account that gets left alone instead of retried.
"""
import hashlib
import json
import secrets
import sqlite3
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

SERVER = Path(__file__).resolve().parent.parent / "server"
# The server is the operator's own box — it holds the keys — so it is not part of
# the published repository. A skip mark is not enough here: the module is still
# imported while pytest collects, and that alone broke `pytest` for everyone who
# cloned without it. The import itself is the condition.
sys.path.insert(0, str(SERVER))
try:
    import ed25519       # noqa: E402  the pool's own copy, same file as the client's
    import pool_server  # noqa: E402
except ImportError:
    pytest.skip("the pool server is not in this checkout", allow_module_level=True)

KEYS = {"groq": ["gsk_first_secret", "gsk_second_secret"]}
# One install, one seat: the tests sign with this seed unless a case says otherwise.
INSTALL_SEED = bytes(range(32))


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


def enroll(client, base, ip=None, seed=None):
    """Take a seat the way an install does: name the public half of its key."""
    headers = {"X-Forwarded-For": ip} if ip else {}
    seed = INSTALL_SEED if seed is None else seed
    body = json.dumps({"device": ed25519.public_key(seed)[:8].hex(),
                       "public_key": ed25519.public_key(seed).hex()}).encode()
    response = client.post(base + "/v1/enroll", content=body,
                           headers={"Content-Type": "application/json", **headers})
    assert response.status_code == 200, response.text
    return response.json()["token"]


def install(n: int) -> bytes:
    """A different install, for the tests that need to talk about several people."""
    return bytes([n % 251]) * 32


def enroll_raw(client, base, ip=None, seed=None, extra=None):
    """The enrolment request itself, without asserting it was accepted."""
    seed = install(7) if seed is None else seed
    public = ed25519.public_key(seed).hex()
    headers = {"Content-Type": "application/json", **(extra or {})}
    if ip:
        headers["X-Forwarded-For"] = ip
    return client.post(base + "/v1/enroll",
                       content=json.dumps({"device": public[:16], "public_key": public}).encode(),
                       headers=headers)


def signed(token, body: bytes, seed=None, when=None, nonce=None) -> dict:
    """The three headers that make a request provably from this install."""
    seed = INSTALL_SEED if seed is None else seed
    stamp = str(time.time() if when is None else when)
    nonce = nonce or secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    signature = ed25519.sign(seed, f"{stamp}\n{nonce}\n{digest}".encode())
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
            "X-Seat-Timestamp": stamp, "X-Seat-Nonce": nonce,
            "X-Seat-Signature": signature.hex()}


def complete(client, base, token, provider="groq", seed=None, when=None, nonce=None,
             unsigned=False, **body):
    payload = {"model": "llama-3.3-70b", "messages": [{"role": "user", "content": "привет"}],
               "pool_provider": provider}
    payload.update(body)
    raw = json.dumps(payload).encode()
    if unsigned:
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    else:
        headers = signed(token, raw, seed=seed, when=when, nonce=nonce) if token else {}
    return client.post(base + "/v1/chat/completions", content=raw, headers=headers)


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
        # A second seat belongs to a second install, and is not punished for the
        # first one's spending.
        other = enroll(client, pool.base, seed=install(2))
        assert complete(client, pool.base, other, seed=install(2)).status_code == 200


def test_a_forged_forwarded_header_does_not_mint_seats(pool):
    """The whole per-IP limit used to rest on a header the caller writes.

    Nothing here spoofs an address: every request arrives from 127.0.0.1, and
    without a declared trusted proxy that is what the pool counts.
    """
    with httpx.Client() as client:
        for index in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip=f"8.8.8.{index}", seed=install(index + 1))
        blocked = enroll_raw(client, pool.base, ip="8.8.8.99", seed=install(99))
        assert blocked.status_code == 429


def test_a_declared_proxy_is_the_only_one_whose_header_is_believed(pool, monkeypatch):
    """With a trusted proxy in front, the counted address is the client's — so one
    client keeps its limit and the building next to it is not punished for it."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.1")
    with httpx.Client() as client:
        for index in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip="203.0.113.7", seed=install(index + 1))
        assert enroll_raw(client, pool.base, ip="203.0.113.7",
                          seed=install(9)).status_code == 429
        assert enroll_raw(client, pool.base, ip="198.51.100.3",
                          seed=install(9)).status_code == 200


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


def test_the_pool_forwards_chat_completions_and_nothing_else(pool):
    """Image, video, audio and embedding calls are what gets an account reported,
    so they are refused by name — before a key is leased and before the provider
    is picked, which is why a valid `pool_provider` does not open the door."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        for model in ("seedream-5", "qwen-image-2.0-pro", "qwen-video",
                      "text-embedding-3-small", "whisper-1", "tts-1"):
            before = len(pool.calls)
            answer = complete(client, pool.base, token, model=model)
            assert answer.status_code == 400, model
            assert "chat completions only" in answer.json()["error"], model
            assert len(pool.calls) == before, f"{model} reached the upstream"


def test_a_chat_model_is_not_collateral_of_the_refusal(pool):
    """The refusal matches shapes, not letters: the same name that carries a real
    chat model must still be served, or the filter would starve the pool."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, model="deepseek-v4")
        assert answer.status_code == 200
        assert pool.calls[-1]["body"]["model"] == "deepseek-v4"


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
#
# This file exists twice, with the same content: here, where the operator keeps
# `server/`, and in the private repository that actually runs the pool. These three
# need the BeeCode client as well, so where it is not installed they skip instead
# of turning the whole file into an import error.

def test_the_provider_talks_to_the_pool_and_gets_the_answer(pool):
    import asyncio

    pytest.importorskip("beeagent", reason="the BeeCode client lives in another repository")
    from beeagent.providers.pool import PoolProvider, enroll

    token = enroll(pool.base)["token"]
    provider = PoolProvider(url=pool.base, token=token)
    answer = asyncio.run(provider.chat([{"role": "user", "content": "привет"}], "llama-3.3-70b"))
    assert answer == "жужж"
    assert pool.calls[-1]["key"] == "gsk_first_secret"


def test_the_provider_keeps_thinking_apart_from_the_answer(pool, monkeypatch):
    import asyncio

    pytest.importorskip("beeagent", reason="the BeeCode client lives in another repository")
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

    pytest.importorskip("beeagent", reason="the BeeCode client lives in another repository")
    from beeagent.providers.pool import PoolProvider

    provider = PoolProvider(url=pool.base, token="")
    with pytest.raises(Exception) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert "/pool enroll" in str(raised.value)


def test_a_key_that_spent_todays_ceiling_is_not_offered_again(pool):
    """One account walking into the provider's own daily wall is the failure that
    costs the key, so the pool stops using it first — and when all of them are
    spent it says so with the wait until midnight, not a retry every few seconds."""
    def keys_of(report):
        return sorted(k["used_today"] for k in report["keys"])

    with httpx.Client() as client:
        token = enroll(client, pool.base)
        ceiling = client.post(pool.base + "/v1/admin/key_ceiling",
                              json={"provider": "groq", "tokens": 1},
                              headers={"X-Admin": "admin-secret"})
        assert ceiling.status_code == 200, "the route is not swallowed by the seat /limit"

        assert complete(client, pool.base, token).status_code == 200
        report = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secret"}).json()
        used = keys_of(report)
        assert used[0] == 0 and used[1] > 0, "the second account was not touched"

        assert complete(client, pool.base, token).status_code == 200
        report = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secret"}).json()
        assert min(keys_of(report)) >= 1, report["keys"]

        spent = complete(client, pool.base, token)
        assert spent.status_code == 429
        assert "ceiling" in spent.json()["error"]
        assert spent.json()["resets_in_seconds"] > 60, "the wait is until midnight"
        assert len(pool.calls) == 2, "a spent pool must not knock on the provider again"


def test_the_ceiling_rolls_over_with_the_day(pool, monkeypatch):
    """A key is skipped on `day = today`, so a row written yesterday has spent
    nothing — and the report has to agree, or the operator sees a spent pool."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        complete(client, pool.base, token)
        client.post(pool.base + "/v1/admin/key_ceiling", json={"provider": "groq", "tokens": 1},
                    headers={"X-Admin": "admin-secret"})
        monkeypatch.setattr(pool_server, "today", lambda: "1999-01-01")
        assert pool_server.at_today_ceiling("groq") is False
        assert complete(client, pool.base, token).status_code == 200, "a new day, a usable key"


def test_one_number_can_give_every_key_the_same_ceiling(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "50")
    pool_server.sync_keys()
    with httpx.Client() as client:
        report = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secret"}).json()
    assert [k["daily_limit"] for k in report["keys"]] == [50, 50]


def test_the_keys_can_arrive_as_an_environment_value(monkeypatch):
    """A PaaS hands out secrets as environment values, and Render's disk is wiped
    on every deploy — so requiring a file first would mean committing one."""
    monkeypatch.setenv("BEECODE_POOL_KEYS", '{"groq": ["gsk_env_secret", "  "]}')
    assert pool_server.keys_from_everywhere("nowhere.json") == {"groq": ["gsk_env_secret"]}


def test_an_unreadable_or_unknown_key_blob_is_a_startup_refusal(monkeypatch):
    """Half a JSON or a provider this code has no upstream for must stop the
    start, not leave a pool that quietly answers nothing."""
    monkeypatch.setenv("BEECODE_POOL_KEYS", '{"groq": ["k"}')
    with pytest.raises(SystemExit):
        pool_server.keys_from_everywhere("nowhere.json")
    monkeypatch.setenv("BEECODE_POOL_KEYS", '{"somewhere-else": ["k"]}')
    with pytest.raises(SystemExit):
        pool_server.keys_from_everywhere("nowhere.json")


def test_a_seat_is_told_which_address_it_was_recorded_under(pool):
    """Without this, a proxy chain read wrongly looks like a broken pool: the
    number is the caller's own address, and it is the first thing to check."""
    with httpx.Client() as client:
        seat = enroll_raw(client, pool.base).json()
        assert seat["seen_from"] == "127.0.0.1"
        assert "debug" not in seat, "the internals stay off unless asked for"


def test_the_debug_block_shows_the_chain_the_proxy_actually_wrote(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_DEBUG", "1")
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.0/8")
    with httpx.Client() as client:
        seat = enroll_raw(client, pool.base, ip="203.0.113.9, 10.192.0.7").json()
    assert seat["debug"]["forwarded_for"] == "203.0.113.9, 10.192.0.7"
    assert seat["seen_from"] == "10.192.0.7", "the rightmost hop is the one we were told to trust"


def test_behind_cloudflare_the_address_that_counts_is_the_one_it_verified(pool, monkeypatch):
    """Measured on Render: the forwarded chain ends in shared edge hops, so the
    rightmost-public-hop rule records a Cloudflare address instead of a person —
    and three seats per address stops meaning three seats per install."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.0/8, 10.0.0.0/8")
    with httpx.Client() as client:
        seat = enroll_raw(client, pool.base,
                          ip="146.120.36.40, 172.71.150.29, 10.192.163.192",
                          extra={"CF-Connecting-IP": "146.120.36.40"}).json()
        assert seat["seen_from"] == "146.120.36.40"


def test_a_forwarded_address_is_not_believed_from_a_stranger(pool, monkeypatch):
    """CF-Connecting-IP is only the truth when it arrives from the proxy we were
    told to trust. From anywhere else it is a header anyone can write."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "10.0.0.0/8")   # not loopback
    with httpx.Client() as client:
        response = client.post(pool.base + "/v1/enroll",
                               content=json.dumps({"device": "aa" * 8, "public_key":
                                                   ed25519.public_key(install(3)).hex()}).encode(),
                               headers={"Content-Type": "application/json",
                                        "CF-Connecting-IP": "146.120.36.40"})
        assert response.json()["seen_from"] == "127.0.0.1", "the socket peer is what we saw"


def test_the_pool_can_cap_seats_per_day_whatever_the_addresses_say(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_ENROLLS_PER_DAY", "2")
    with httpx.Client() as client:
        assert enroll_raw(client, pool.base, seed=install(11)).status_code == 200
        assert enroll_raw(client, pool.base, seed=install(12)).status_code == 200
        third = enroll_raw(client, pool.base, seed=install(13))
        assert third.status_code == 429
        assert "seats" in third.json()["error"] or "today" in third.json()["error"]


def test_an_unsigned_request_never_reaches_the_provider(pool):
    """The point of the install key: a seat token copied out of beeagent.json is
    not enough to spend anything, and a request that cannot be vouched for must
    cost nothing — not even one call upstream."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        refused = complete(client, pool.base, token, unsigned=True)
        assert refused.status_code == 401
        assert "install" in refused.json()["error"]
        assert pool.calls == [], "nothing left this box"


def test_a_seat_signed_by_another_install_is_refused(pool):
    """Someone else's key file is not this one's; the seat belongs where it was born."""
    with httpx.Client() as client:
        token = enroll(client, pool.base, seed=install(21))
        stolen = complete(client, pool.base, token, seed=install(22))
        assert stolen.status_code == 401
        assert pool.calls == []


def test_a_captured_request_cannot_be_sent_twice(pool):
    """The signature covers a nonce and a timestamp, so a copy of a real request —
    from a proxy log or a friend's terminal — is worth one use, not two."""
    with httpx.Client() as client:
        token = enroll(client, pool.base, seed=install(31))
        once = complete(client, pool.base, token, seed=install(31), nonce="aa" * 16)
        twice = complete(client, pool.base, token, seed=install(31), nonce="aa" * 16)
        assert once.status_code == 200
        assert twice.status_code == 401
        assert "replay" in twice.json()["error"] or "already" in twice.json()["error"]
        assert len(pool.calls) == 1


def test_an_old_request_dies_with_the_window(pool):
    """Without an expiry, a signature is a permanent password: valid forever, once
    captured. Two minutes is what the nonce table remembers."""
    with httpx.Client() as client:
        token = enroll(client, pool.base, seed=install(41))
        stale = complete(client, pool.base, token, seed=install(41),
                         when=time.time() - pool_server.SKEW_SECONDS - 60)
        assert stale.status_code == 401
        assert "clock" in stale.json()["error"] or "timestamp" in stale.json()["error"]


def test_one_install_one_seat_and_a_keyless_enrolment_refused(pool):
    """Re-enrolling from the same machine must not mint a second seat — that is how
    a shared key file quietly multiplies against the daily caps."""
    with httpx.Client() as client:
        first = enroll(client, pool.base, seed=install(51))
        again = enroll_raw(client, pool.base, seed=install(51))
        assert again.json()["token"] == first, "the same install gets its same seat"
        assert again.json().get("reused") is True
        bare = client.post(pool.base + "/v1/enroll", content=b"{}",
                           headers={"Content-Type": "application/json"})
        assert bare.status_code == 400, "no install key, no seat"
