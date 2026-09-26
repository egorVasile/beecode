"""The pool server's promises, tested against the real HTTP server.

Everything here is about the parts that must not be broken: a key that never
leaves the box, a stranger who cannot spend the budget, and a rate-limited
account that gets left alone instead of retried.
"""
import hashlib
import json
import os
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
# These tests drive a real HTTP server, and the database behind it is whichever
# one the environment names. Against a Postgres in another country a single
# request takes seconds rather than milliseconds, so the client waits; on the
# platform the pool actually runs, both are in the same region and this is never
# noticed. The default five seconds is a local assumption, not a promise.
CLIENT_TIMEOUT = 60.0


@pytest.fixture()
def pool(tmp_path, monkeypatch):
    """A live server on a random port, with the upstream replaced by a recorder."""
    # A file database is new every test by construction; a Postgres one is shared
    # and this fixture empties it. Running the suite against the production
    # database would delete every seat and every counter in it, so the suite
    # refuses unless the name says it is a test database.
    url = os.environ.get("BEECODE_POOL_PG_URL") or ""
    if url and not _is_test_database(url):
        pytest.skip(f"refusing to wipe the database behind BEECODE_POOL_PG_URL "
                    f"({_database_of(url)}); name it test_* to run against Postgres")
    pool_server._state["db"] = pool_server.open_db(str(tmp_path / "pool.db"))
    # A file database is new every test by construction; a Postgres one is the
    # same tables all run long, so the fixture has to empty it — otherwise seats
    # from an earlier test trip the per-IP limits of a later one, and the failure
    # points at nothing.
    for table in ("tokens", "keys", "events", "seen_nonces"):
        pool_server._state["db"].execute(f"DELETE FROM {table}")
    pool_server._state["db"].commit()
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


def _database_of(url: str) -> str:
    from urllib.parse import urlparse
    return (urlparse(url).path or "/").lstrip("/")


def _is_test_database(url: str) -> bool:
    return _database_of(url).startswith("test")


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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        body = client.get(pool.base + "/healthz").json()
    assert body == {"ok": True}


def test_a_completions_request_needs_a_seat(pool):
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        assert complete(client, pool.base, "").status_code == 401
        assert complete(client, pool.base, "made-up-token").status_code == 401
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token)
        assert answer.status_code == 200
        assert answer.json()["choices"][0]["message"]["content"] == "жужж"


def test_the_key_never_leaves_the_server_at_all(pool):
    """Not even its tail: that was a stable id for one account across every seat,
    which let a stranger aim at an account and measure the pool through seats."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        body = complete(client, pool.base, token).json()
    assert "pool_key" not in body
    assert "cret" not in json.dumps(body)
    assert body["pool_lane"].startswith("groq-")
    assert pool.calls[0]["key"] == "gsk_first_secret", "the upstream really got the key"


def test_a_rate_limited_key_is_cooled_down_and_the_next_one_tried(pool):
    pool.answers[:] = [(429, '{"error":"rate limit"}'),
                       (200, json.dumps({"choices": [{"message": {"content": "со второго"}}]}))]
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token)
    assert answer.status_code == 429
    assert int(answer.headers.get("Retry-After", 0)) > 0


def test_the_daily_budget_is_enforced_per_seat(pool):
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        for index in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip=f"8.8.8.{index}", seed=install(index + 1))
        blocked = enroll_raw(client, pool.base, ip="8.8.8.99", seed=install(99))
        assert blocked.status_code == 429


def test_a_declared_proxy_is_the_only_one_whose_header_is_believed(pool, monkeypatch):
    """With a trusted proxy in front, the counted address is the client's — so one
    client keeps its limit and the building next to it is not punished for it."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.1")
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        for index in range(pool_server.ENROLLS_PER_IP_PER_DAY):
            enroll(client, pool.base, ip="203.0.113.7", seed=install(index + 1))
        assert enroll_raw(client, pool.base, ip="203.0.113.7",
                          seed=install(9)).status_code == 429
        assert enroll_raw(client, pool.base, ip="198.51.100.3",
                          seed=install(9)).status_code == 200


def test_a_client_cannot_point_the_pool_at_an_arbitrary_url(pool):
    """The model decides which account answers; a provider named by the client is
    only honoured when it is one of the fixed upstreams, and never a URL."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, provider="http://169.254.169.254/")
        assert answer.status_code == 200, "the model still resolved to a real provider"
        assert pool.calls[-1]["provider"] == "groq"
        assert pool.calls[-1]["key"] == "gsk_first_secret"


def test_a_prompt_bigger_than_the_pool_accepts_is_refused_before_parsing(pool):
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, model="deepseek-v4")
        assert answer.status_code == 200
        assert pool.calls[-1]["body"]["model"] == "deepseek-v4"


def test_admin_endpoints_need_the_admin_token(pool):
    """The accepted channel is one header: `X-Admin`, and nothing else."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        assert client.get(pool.base + "/v1/pool").status_code == 401
        report = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secret"})
        assert report.status_code == 200
        assert "gsk_first_secret" not in report.text
        token = enroll(client, pool.base)
        revoked = client.post(pool.base + "/v1/admin/revoke", json={"token": token},
                              headers={"X-Admin": "admin-secret"})
        assert revoked.status_code == 200
        assert complete(client, pool.base, token).status_code == 401, "a revoked seat is gone"


def test_a_wrong_admin_token_is_not_the_same_failure_as_sending_none(pool):
    """The distinction an operator needs mid-incident, in the status code alone:

    401 — the header never arrived, so the caller's command is wrong;
    403 — it arrived and is not what this box holds, so a note and the
          dashboard disagree;
    and no other route's answer is confusable with either, because auth is
    decided before routing.
    """
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        missing = client.get(pool.base + "/v1/pool")
        blank = client.get(pool.base + "/v1/pool", headers={"X-Admin": ""})
        wrong = client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secre"})
        assert missing.status_code == 401, missing.text
        assert blank.status_code == 401, "an empty header is the same as no header"
        assert wrong.status_code == 403, wrong.text
        assert missing.json()["refused"] != wrong.json()["refused"], "the reason differs"
        assert "X-Admin" in missing.json()["error"], "say which header to send"
        assert missing.headers.get("www-authenticate") == "X-Admin"
        assert "not the one this pool holds" in wrong.json()["error"]
        assert wrong.headers.get("www-authenticate") is None

        post_missing = client.post(pool.base + "/v1/admin/revoke", json={"token": "x"})
        post_wrong = client.post(pool.base + "/v1/admin/revoke", json={"token": "x"},
                                 headers={"X-Admin": "not-the-token"})
        assert (post_missing.status_code, post_wrong.status_code) == (401, 403)
        unknown = client.post(pool.base + "/v1/admin/nosuchroute", json={},
                              headers={"X-Admin": "admin-secret"})
        assert unknown.status_code == 404, "only an accepted request learns the path is wrong"


def test_a_box_with_no_admin_secret_closes_the_routes_and_says_so(pool, monkeypatch):
    """_unset used to answer the same 403 as a typoed token, which is how an
    operator spends an afternoon re-typing a correct secret. Routes stay closed;
    only the lie changes — this is a configuration the server is missing, not a
    caller doing something wrong, so it cannot be a 403 at all.
    """
    monkeypatch.delenv("BEECODE_POOL_ADMIN", raising=False)
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        for headers in ({}, {"X-Admin": "admin-secret"}, {"X-Admin": "anything-at-all"}):
            closed = client.get(pool.base + "/v1/pool", headers=headers)
            assert closed.status_code == 503, (headers, closed.text)
            assert closed.json()["refused"] == "no-secret"
            assert "BEECODE_POOL_ADMIN" in closed.json()["error"], "name the fix"
        posted = client.post(pool.base + "/v1/admin/revoke", json={"token": "x"},
                             headers={"X-Admin": "admin-secret"})
        assert posted.status_code == 503
        # Seats are still minted and spent by their own tokens; only the operator
        # doors are shut.
        assert client.get(pool.base + "/v1/seat").status_code == 401


def test_every_admin_refusal_is_logged_with_its_own_reason(pool):
    """The codes tell the caller; the log tells the operator what actually
    happened on a request they are not holding. Both offered and held values are
    recorded masked, so "I typoed" and "the secret never arrived" differ there
    too — without the log ever becoming a copy of the secret."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        client.get(pool.base + "/v1/pool")
        client.get(pool.base + "/v1/pool", headers={"X-Admin": "admin-secre"})
    monkey = pool.db.execute("SELECT note FROM events WHERE kind=? ORDER BY ts",
                             ("admin-denied",)).fetchall()
    notes = [row["note"] for row in monkey]
    assert len(notes) == 2, "one line per refusal, in order"
    assert "nothing sent in X-Admin" in notes[0]
    assert "does not match" in notes[1] and "offered" in notes[1]
    assert notes[0] != notes[1], "the reasons differ in the log, not just on the wire"
    for note in notes:
        assert "admin-secret" not in note, "a refusal note carries tails, never values"
        assert "admin-secre" not in note, "not even the rejected one"
    assert pool_server._mask("admin-secret") in notes[1], "the tail is enough to compare"


def test_the_admin_secret_is_compared_in_constant_time(pool, monkeypatch):
    """No prefix shortcut, and no `==` on text either.

    The spy asserts the shape of the comparison the code promises: both whole
    values as bytes, once per request. A prefix or `startswith` check would show
    up here as a call that never sees the full secret, and `str` arguments would
    raise TypeError on a non-ASCII value instead of answering.
    """
    seen = []
    real = pool_server.secrets.compare_digest

    def spy(left, right):
        seen.append((left, right))
        return real(left, right)

    monkeypatch.setattr(pool_server.secrets, "compare_digest", spy)
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        assert client.get(pool.base + "/v1/pool",
                          headers={"X-Admin": "admin-secre"}).status_code == 403
        assert client.get(pool.base + "/v1/pool",
                          headers={"X-Admin": "admin-secret"}).status_code == 200
    assert seen, "the gate really is the comparison"
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in seen)
    assert seen[0] == (b"admin-secret", b"admin-secre"), "full values, both sides"
    assert len(seen) == 2, "one comparison per request, however much differs"


def test_a_secret_that_cannot_arrive_is_named_at_startup(pool, monkeypatch):
    """Header bytes cannot carry a non-ASCII value the way this box compares it,
    so such a secret is a lockout the configuration built and the operator gets
    blamed for. It has to be said before anyone is locked out by it."""
    monkeypatch.setenv("BEECODE_POOL_ADMIN", "ключ-секрет")
    notes = pool_server.admin_config_notes()
    assert any("ASCII" in note for note in notes), notes
    assert not any("ключ" in note or "секрет" in note for note in notes), "nothing printed"
    monkeypatch.setenv("BEECODE_POOL_ADMIN", "ascii-value-9f")
    assert not any("WARNING" in note for note in pool_server.admin_config_notes())
    assert pool_server._mask("ascii-value-9f") in pool_server.admin_config_notes()[0]
    monkeypatch.delenv("BEECODE_POOL_ADMIN", raising=False)
    assert "unset" in pool_server.admin_config_notes()[0]


def test_the_operator_routes_are_matched_whatever_shape_the_url_arrives_in(pool):
    """/v1/pool/ and /v1/pool?... used to answer 404 "unknown path", which an
    operator reads as "this build has no such route" — the same dead end as the
    bare 403, one step further along. Only the matching is forgiving; the token
    is still header-only, because a secret in a URL lands in access logs."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        for url in ("/v1/pool", "/v1/pool/", "/v1/pool?days=1"):
            answer = client.get(pool.base + url, headers={"X-Admin": "admin-secret"})
            assert answer.status_code == 200, (url, answer.text)
        in_query = client.get(pool.base + "/v1/pool?X-Admin=admin-secret")
        assert in_query.status_code == 401, "a token in the URL is not a token sent"
        cooled = client.post(pool.base + "/v1/admin/cooldown/",
                             json={"provider": "groq", "seconds": 1},
                             headers={"X-Admin": "admin-secret"})
        assert cooled.status_code == 200, cooled.text


def test_a_seat_awaiting_approval_is_not_served(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_APPROVAL", "1")
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        assert complete(client, pool.base, token).status_code == 403
        client.post(pool.base + "/v1/admin/approve", json={"token": token},
                    headers={"X-Admin": "admin-secret"})
        assert complete(client, pool.base, token).status_code == 200


def test_the_budget_rolls_over_when_the_day_changes(pool):
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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

    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        complete(client, pool.base, token)
        client.post(pool.base + "/v1/admin/key_ceiling", json={"provider": "groq", "tokens": 1},
                    headers={"X-Admin": "admin-secret"})
        monkeypatch.setattr(pool_server, "today", lambda: "1999-01-01")
        assert pool_server.at_today_ceiling("groq") is False
        assert complete(client, pool.base, token).status_code == 200, "a new day, a usable key"


def test_an_operator_can_roll_the_day_back_without_waiting_for_midnight(pool):
    """The repair for a day that was spent twice, and no wider than that.

    A deploy of the rollover fix does not unlock the rows the old code already
    stamped, so the box stays shut until midnight unless somebody says otherwise.
    Rolling the stamp is the whole route: the ceiling itself is untouched, so a
    pool whose accounts the upstream really did spend says so again on the very
    next request rather than being walked into the wall a second time.
    """
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        client.post(pool.base + "/v1/admin/key_ceiling", json={"provider": "groq", "tokens": 1},
                    headers={"X-Admin": "admin-secret"})
        assert complete(client, pool.base, token).status_code == 200
        assert complete(client, pool.base, token).status_code == 200
        stuck = complete(client, pool.base, token)
        assert stuck.status_code == 429 and "ceiling" in stuck.json()["error"], stuck.json()

        refused = client.post(pool.base + "/v1/admin/roll_keys", json={})
        assert refused.status_code in (401, 403), "the route answers to strangers"

        rolled = client.post(pool.base + "/v1/admin/roll_keys", json={},
                             headers={"X-Admin": "admin-secret"})
        assert rolled.status_code == 200, rolled.json()
        assert rolled.json()["rolled"] == 2, rolled.json()
        assert pool_server.at_today_ceiling("groq") is False, "the day is rolled"
        # The keys were handed back a moment ago, and a hand-back leaves the lease
        # stamp for a second on purpose. Waiting that out is what the operator sees;
        # rolling a day must not clear a real 429 cooldown, because that cooldown is
        # the one thing standing between this pool and the provider's wall.
        time.sleep(1.5)
        assert complete(client, pool.base, token).status_code == 200, "the pool is usable again"


def test_one_number_can_give_every_key_the_same_ceiling(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "50")
    pool_server.sync_keys()
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        seat = enroll_raw(client, pool.base).json()
        assert seat["seen_from"] == "127.0.0.1"
        assert "debug" not in seat, "the internals stay off unless asked for"


def test_the_debug_block_shows_the_chain_the_proxy_actually_wrote(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_DEBUG", "1")
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.0/8")
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        seat = enroll_raw(client, pool.base, ip="203.0.113.9, 10.192.0.7").json()
    assert seat["debug"]["forwarded_for"] == "203.0.113.9, 10.192.0.7"
    assert seat["seen_from"] == "10.192.0.7", "the rightmost hop is the one we were told to trust"


def test_behind_cloudflare_the_address_that_counts_is_the_one_it_verified(pool, monkeypatch):
    """Measured on Render: the forwarded chain ends in shared edge hops, so the
    rightmost-public-hop rule records a Cloudflare address instead of a person —
    and three seats per address stops meaning three seats per install."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "127.0.0.0/8, 10.0.0.0/8")
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        seat = enroll_raw(client, pool.base,
                          ip="146.120.36.40, 172.71.150.29, 10.192.163.192",
                          extra={"CF-Connecting-IP": "146.120.36.40"}).json()
        assert seat["seen_from"] == "146.120.36.40"


def test_a_forwarded_address_is_not_believed_from_a_stranger(pool, monkeypatch):
    """CF-Connecting-IP is only the truth when it arrives from the proxy we were
    told to trust. From anywhere else it is a header anyone can write."""
    monkeypatch.setenv("BEECODE_POOL_TRUSTED_PROXY", "10.0.0.0/8")   # not loopback
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        response = client.post(pool.base + "/v1/enroll",
                               content=json.dumps({"device": "aa" * 8, "public_key":
                                                   ed25519.public_key(install(3)).hex()}).encode(),
                               headers={"Content-Type": "application/json",
                                        "CF-Connecting-IP": "146.120.36.40"})
        assert response.json()["seen_from"] == "127.0.0.1", "the socket peer is what we saw"


def test_the_pool_can_cap_seats_per_day_whatever_the_addresses_say(pool, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_ENROLLS_PER_DAY", "2")
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        assert enroll_raw(client, pool.base, seed=install(11)).status_code == 200
        assert enroll_raw(client, pool.base, seed=install(12)).status_code == 200
        third = enroll_raw(client, pool.base, seed=install(13))
        assert third.status_code == 429
        assert "seats" in third.json()["error"] or "today" in third.json()["error"]


def test_an_unsigned_request_never_reaches_the_provider(pool):
    """The point of the install key: a seat token copied out of beeagent.json is
    not enough to spend anything, and a request that cannot be vouched for must
    cost nothing — not even one call upstream."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        refused = complete(client, pool.base, token, unsigned=True)
        assert refused.status_code == 401
        assert "install" in refused.json()["error"]
        assert pool.calls == [], "nothing left this box"


def test_a_seat_signed_by_another_install_is_refused(pool):
    """Someone else's key file is not this one's; the seat belongs where it was born."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base, seed=install(21))
        stolen = complete(client, pool.base, token, seed=install(22))
        assert stolen.status_code == 401
        assert pool.calls == []


def test_a_captured_request_cannot_be_sent_twice(pool):
    """The signature covers a nonce and a timestamp, so a copy of a real request —
    from a proxy log or a friend's terminal — is worth one use, not two."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
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
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base, seed=install(41))
        stale = complete(client, pool.base, token, seed=install(41),
                         when=time.time() - pool_server.SKEW_SECONDS - 60)
        assert stale.status_code == 401
        assert "clock" in stale.json()["error"] or "timestamp" in stale.json()["error"]


def test_one_install_one_seat_and_a_keyless_enrolment_refused(pool):
    """Re-enrolling from the same machine must not mint a second seat — that is how
    a shared key file quietly multiplies against the daily caps."""
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        first = enroll(client, pool.base, seed=install(51))
        again = enroll_raw(client, pool.base, seed=install(51))
        assert again.json()["token"] == first, "the same install gets its same seat"
        assert again.json().get("reused") is True
        bare = client.post(pool.base + "/v1/enroll", content=b"{}",
                           headers={"Content-Type": "application/json"})
        assert bare.status_code == 400, "no install key, no seat"


def test_the_model_list_is_a_seat_privilege_and_lists_only_chat(pool, monkeypatch):
    """A stranger should not get to read which accounts the pool holds, and
    neither should a seat see the image models it is not allowed to ask."""
    monkeypatch.setattr(pool_server, "list_upstream_models",
                        lambda provider, key: ["qwen3.8-max", "seedream-5", "gpt-5-6-luna"])
    with httpx.Client() as client:
        assert client.get(pool.base + "/v1/models").status_code == 401
        token = enroll(client, pool.base)
        body = client.get(pool.base + "/v1/models",
                          headers={"Authorization": f"Bearer {token}"}).json()
    assert body["data"] == [{"id": "qwen3.8-max"}, {"id": "gpt-5-6-luna"}]


def test_a_provider_that_does_not_answer_the_list_is_said_so(pool, monkeypatch):
    """An empty list would look like "this pool has no models", which is a
    different fact and a different fix."""
    monkeypatch.setattr(pool_server, "list_upstream_models", lambda provider, key: [])
    pool_server._models_cache["at"] = 0.0
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = client.get(pool.base + "/v1/models",
                            headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 502
        assert "did not answer" in answer.json()["error"]


def test_a_refusal_names_the_model_that_refused(pool):
    """The difference between "the pool is broken" and "this one model is broken"
    is a name. Without it the user has nothing to act on but a 502."""
    pool.answers[:] = [(502, '{"error":"bad gateway"}')]
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, model="qwen3.8-max")
        assert answer.status_code == 502
        body = answer.json()
    assert "qwen3.8-max" in body["error"], body
    assert "/model" in body["error"], "say what to do about it"
    assert body["model"] == "qwen3.8-max"


def test_a_refusal_carries_the_reason_the_provider_gave(pool):
    """The account says why — and the pool used to throw the sentence away.

    Diagnosing a dead pool from the outside means reading a note that said "the
    account answered, and not with an answer": true of a wrong model name, of a
    revoked key and of a provider mid-deploy, and useless for telling them apart.
    The provider's own message now travels to the seat and into the event — with
    any credential it happens to echo cut out, because a key pasted into an error
    body is a key in the log.
    """
    pool.answers[:] = [(400, '{"error":{"message":"invalid api key '
                             'sk-abcdefghij0123456789 for this account"}}')]
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, model="gemma-3-12b")
        body = answer.json()
    assert answer.status_code == 502
    assert "invalid api key" in body["error"], body
    assert "invalid api key" in body["upstream_detail"], body
    assert "sk-abcdefghij0123456789" not in json.dumps(body), "the key reached the seat"
    logged = pool.db.execute(
        "SELECT note FROM events WHERE kind='provider-refused'").fetchall()
    notes = [row[0] if not isinstance(row, dict) else row["note"] for row in logged]
    assert any("invalid api key" in str(n) for n in notes), notes
    assert all("sk-abcdefghij0123456789" not in str(n) for n in notes), notes


class InterfaceError(Exception):
    """Named like pg8000's, which is how store.py recognises a dead socket."""


def _detached_store(replacement, initial=None):
    """A PostgresConnection with its socket swapped, so no database is involved."""
    import store

    obj = object.__new__(store.PostgresConnection)
    obj.url = "postgresql://someone@else/neondb"
    obj._gate = threading.RLock()
    obj._db = initial if initial is not None else Dead()
    obj._commit = obj._db.commit
    obj._rollback = obj._db.rollback
    obj._reopened = 0

    def _reconnect():
        obj._reopened += 1
        obj._db = replacement
        obj._commit = replacement.commit
        obj._rollback = replacement.rollback

    obj._connect = _reconnect
    return obj


class Dead:
    """Every statement over it fails the way a closed socket fails."""

    def cursor(self):
        raise InterfaceError("network error")

    def commit(self):
        raise InterfaceError("network error")

    def rollback(self):
        raise InterfaceError("network error")


class Alive:
    def __init__(self):
        self.statements = []

    def cursor(self):
        outer = self

        class _C:
            description = [("n",)]

            def execute(self, sql, params=()):
                outer.statements.append(sql)

            def fetchall(self):
                return [(1,)]

        return _C()

    def commit(self):
        pass

    def rollback(self):
        pass


def test_a_dropped_database_socket_is_reopened_rather_than_failing_forever():
    """Neon closes an idle connection and a free instance sleeps.

    Both leave the socket opened at boot dead, and the pool would answer every
    later request with a network error forever -- which is exactly how it looked
    from outside: 502 with no body, and `/models` reporting no models.
    """
    alive = Alive()
    conn = _detached_store(alive)

    result = conn.execute("SELECT count(*) FROM tokens").fetchone()

    assert conn._reopened == 1, "the dead socket should have been replaced once"
    assert alive.statements == ["SELECT count(*) FROM tokens"]
    assert result["n"] == 1


def test_a_statement_the_database_rejected_is_not_retried():
    """A bad query fails the same way forever, so reconnecting only hides it."""

    class Wrong:
        def cursor(self):
            class _C:
                description = None

                def execute(self, sql, params=()):
                    raise ValueError("operator does not exist: text = date")

                def fetchall(self):
                    return []

            return _C()

        def commit(self):
            pass

        def rollback(self):
            pass

    conn = _detached_store(Wrong(), initial=Wrong())
    with pytest.raises(ValueError):
        conn.execute("SELECT nonsense")
    assert conn._reopened == 0


def test_a_crashing_handler_still_answers_json(pool, monkeypatch):
    """An exception that escapes the handler becomes an empty 502 at Cloudflare.

    That is what made this outage unreadable: the client saw no models and no
    reason, because nothing ever explained itself.
    """

    def boom(token):
        raise RuntimeError("database link dropped")

    monkeypatch.setattr(pool_server, "seat_for", boom)
    with httpx.Client() as client:
        answer = client.get(pool.base + "/v1/models",
                            headers={"Authorization": "Bearer whatever"})
    assert answer.status_code == 502
    assert "error" in answer.json(), "the body must say something"
    assert "RuntimeError" in answer.json()["error"]


def test_the_measured_list_survives_a_provider_that_will_not_answer(pool, monkeypatch):
    """A seat should not lose the catalogue because the listing call failed.

    crax blocks some networks outright, so `list_upstream_models` returning []
    means "could not ask", not "has nothing" — and the measured ids are the answer
    either way. Asserted against `MEASURED_CHAT` rather than named ids on purpose:
    the endpoint rewrites its catalogue without asking (2026-09-26: twelve of the
    thirteen names carried here stopped existing), and a test that recites the old
    list is a test that fails on the day the fix works.
    """
    monkeypatch.setattr(pool_server, "list_upstream_models", lambda p, k: [])
    monkeypatch.setitem(pool_server._state, "keys", {"crax": ["crk_live_test"]})
    monkeypatch.setitem(pool_server._models_cache, "at", 0.0)
    monkeypatch.setitem(pool_server._models_cache, "ids", [])

    ids, note = pool_server.cached_models()

    assert note == ""
    assert ids == list(pool_server.MEASURED_CHAT["crax"]), ids
    assert "seedream-5" not in ids, "an image model never belongs in a chat list"


def test_an_offered_model_stays_on_the_account_that_was_measured_on_it():
    """Routing used to be decided by the order MODEL_HINTS was written in.

    `qwen` (openrouter) is written before `qwen3-coder`, and both fit
    `qwen3-coder-480b`, so the shorter hint won. With only one provider loaded the
    one-provider fallback covered it up, and the models kept answering -- which is
    why this went unseen. Add a second provider's keys and every measured crax
    model silently moves to an account that was never measured for it: same code,
    same models, different answers.
    """
    saved = dict(pool_server._state["keys"])
    try:
        pool_server._state["keys"] = {"crax": ["c"], "groq": ["g"], "together": ["t"],
                                      "openrouter": ["o"], "cerebras": ["e"]}
        for provider, measured in pool_server.MEASURED_CHAT.items():
            for model in measured:
                assert pool_server.provider_for(model) == provider, \
                    f"{model} left {provider} for {pool_server.provider_for(model)}"
    finally:
        pool_server._state["keys"] = saved


def test_a_model_nobody_measured_is_still_routed_by_its_hint():
    """The measured list must not swallow the generic routing."""
    saved = dict(pool_server._state["keys"])
    try:
        pool_server._state["keys"] = {"crax": ["c"], "groq": ["g"]}
        assert pool_server.provider_for("llama-3-8b-instant") == "groq"
    finally:
        pool_server._state["keys"] = saved


def test_a_seat_can_see_its_own_budget(pool):
    """Discovering the limit by getting a 429 mid-task is not a budget."""
    with httpx.Client() as client:
        token = enroll(client, pool.base)
        answer = client.get(pool.base + "/v1/seat", headers={"Authorization": f"Bearer {token}"})
        assert answer.status_code == 200, answer.text
        seat = answer.json()
        nobody = client.get(pool.base + "/v1/seat")
    assert seat["requests_limit"] == pool_server.DEFAULT_REQUESTS_PER_DAY
    assert seat["tokens_limit"] == pool_server.DEFAULT_TOKENS_PER_DAY
    assert seat["resets_in_seconds"] > 0
    assert nobody.status_code == 401, "an open /v1/seat would list other people's spend"


def test_the_market_index_is_served_and_names_its_licence(pool, monkeypatch, tmp_path):
    """A marketplace you cannot audit is just a download button.

    Every entry has to carry the licence it was found under, so the client can
    refuse what nobody granted before it reaches a disk.
    """
    index = {"generated": "now", "policy": {"allowed_licenses": ["MIT", "Apache-2.0"]},
             "counts": {"skills": 1},
             "items": {"skills": [{"id": "someone--demo", "name": "demo",
                                   "license": "MIT", "description": "a demo skill",
                                   "sha256": "ab" * 32,
                                   "source": {"repo": "someone/skills", "path": "skills/demo",
                                              "commit": "deadbeef"}}]}}
    path = tmp_path / "index.json"
    path.write_text(json.dumps(index), encoding="utf-8")
    monkeypatch.setenv("BEECODE_POOL_MARKET", str(path))
    with httpx.Client() as client:
        listing = client.get(pool.base + "/v1/market")
        one = client.get(pool.base + "/v1/market/someone--demo")
        missing = client.get(pool.base + "/v1/market/nothing-here")
    assert listing.status_code == 200
    assert listing.json()["items"]["skills"][0]["license"] == "MIT"
    assert one.status_code == 200 and one.json()["id"] == "someone--demo"
    assert missing.status_code == 404


def test_an_ambiguous_market_name_is_refused_not_guessed(pool, monkeypatch, tmp_path):
    """anthropics and openai both publish a skill called `skill-creator`."""
    same = [{"id": "anthropics--skill-creator", "name": "skill-creator", "license": "Apache-2.0"},
            {"id": "openai--skill-creator", "name": "skill-creator", "license": "Apache-2.0"}]
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"items": {"skills": same}}), encoding="utf-8")
    monkeypatch.setenv("BEECODE_POOL_MARKET", str(path))
    with httpx.Client() as client:
        answer = client.get(pool.base + "/v1/market/skill-creator")
    assert answer.status_code == 409, answer.text
    assert sorted(answer.json()["ids"]) == ["anthropics--skill-creator", "openai--skill-creator"]


# --- what the pool owes its own arithmetic ------------------------------------
#
# Each case below came out of a measurement run against a temp SQLite file with
# three fake keys and a stub upstream, not out of reading the code and hoping. The
# shape of every one of them is the same: a number written as if it were a gauge
# and read as if it were a total, or a promise printed by one route and never
# compared by the route that spends.

def free_the_keys(pool):
    """Stand the keys back up without the test waiting out a real lease.

    `release_key` leaves one second of rest on the account and these tests step
    through a day, so a test has to be able to say "some time passed" without
    sleeping for all of it.
    """
    pool.db.execute("UPDATE keys SET cooldown_until=0")
    pool.db.commit()


def limits_of(pool):
    return [r["daily_limit"] for r in
            pool.db.execute("SELECT daily_limit FROM keys ORDER BY id").fetchall()]


STREAM_FRAMES = ['data: {"choices":[{"delta":{"content":"раз"}}]}',
                 'data: {"choices":[{"delta":{"content":"два"}}]}',
                 'data: {"usage":{"prompt_tokens":1000,"completion_tokens":998999,'
                 '"total_tokens":999999}}',
                 'data: [DONE]']


def stream_upstream(lines):
    """A provider that answers a stream, shaped exactly like `_drain` reads one.

    Every line comes back to the pool; only the ones framed as `data:` are forwarded
    to the seat. That asymmetry is the whole bug measured below.
    """
    def call(provider, api_key, body, begin=None, on_chunk=None):
        if begin:
            begin(200)
        if on_chunk:
            for line in lines:
                if line.startswith("data:"):
                    on_chunk(line)
        return 200, "".join(line + "\n" for line in lines)

    return call


def test_a_new_day_starts_a_keys_counter_at_zero(pool, monkeypatch):
    """`used_tokens` is one day's spend, so a new day has to begin at zero.

    It began at yesterday: the release stamped `day=today` on top of the old balance
    and the ceiling filter let the row through for exactly one request, after which
    it was "spent today" with yesterday's number still in it.
    """
    key = pool_server.take_key("groq")
    pool_server.release_key(key["id"], spent=500)
    monkeypatch.setattr(pool_server, "today", lambda: "2000-01-02")
    free_the_keys(pool)
    again = pool_server.take_key("groq")
    assert again["id"] == key["id"] and again["used_tokens"] == 500, "yesterday's balance"
    pool_server.release_key(again["id"], spent=40)
    row = pool.db.execute("SELECT used_tokens, day FROM keys WHERE id=?", (key["id"],)).fetchone()
    assert row["day"] == "2000-01-02"
    assert row["used_tokens"] == 40, \
        f"yesterday's 500 followed the key into today: {row['used_tokens']}"


def test_a_key_that_blew_its_ceiling_yesterday_serves_the_day_after(pool, monkeypatch):
    """The headline: `429 daily_limit` while every account sits idle.

    Three requests of the new day is the shortest honest probe — the first two pass
    even when the counter never resets, because each key gets one free request
    before it locks itself out again.
    """
    monkeypatch.setattr(pool_server, "today", lambda: "2000-01-01")
    pool.answers[:] = [(200, json.dumps({"choices": [{"message": {"content": "да"}}],
                                         "usage": {"total_tokens": 500}}))]
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        client.post(pool.base + "/v1/admin/key_ceiling", json={"provider": "groq", "tokens": 600},
                    headers={"X-Admin": "admin-secret"})
        while True:                             # spend yesterday, on both accounts
            free_the_keys(pool)
            answer = complete(client, pool.base, token)
            if answer.status_code == 429:
                break
            assert answer.status_code == 200, answer.text
        assert answer.json().get("daily_limit") is True, "yesterday is genuinely finished"

        monkeypatch.setattr(pool_server, "today", lambda: "2000-01-02")
        free_the_keys(pool)
        assert pool_server.at_today_ceiling("groq") is False, "a new day is an unspent day"
        for request in (1, 2, 3):
            free_the_keys(pool)
            answer = complete(client, pool.base, token)
            assert answer.status_code == 200, \
                f"day two, request {request}, keys idle: {answer.text}"
        rows = pool.db.execute(
            "SELECT used_tokens FROM keys WHERE provider='groq' ORDER BY id").fetchall()
    assert sorted(r["used_tokens"] for r in rows) == [500, 1000], \
        f"three turns of 500 today, not three turns plus yesterday: {rows}"


def test_releasing_the_same_lease_twice_charges_it_once(pool):
    """A `finally` around the upstream call makes a second release reachable, and a
    plain `used_tokens = used_tokens + spent` would then bill every turn twice."""
    key = pool_server.take_key("groq")
    assert key["lease"] > 0, "the lease travels with the key, or a double release is invisible"
    pool_server.release_key(key["id"], spent=500, lease=key["lease"])
    pool_server.release_key(key["id"], spent=500, lease=key["lease"])
    row = pool.db.execute("SELECT used_tokens FROM keys WHERE id=?", (key["id"],)).fetchone()
    assert row["used_tokens"] == 500, f"one turn charged as {row['used_tokens']}"


def test_a_key_is_handed_back_when_the_upstream_dies_mid_request(pool, monkeypatch):
    """One account, and a provider that closes the socket without answering.

    The release used to sit after the call, so the exception jumped over it and the
    key stayed leased for the whole 120 s. The seat that came next had nothing to
    take and was told to wait 119 seconds for a key nobody was holding.
    """
    monkeypatch.setitem(pool_server._state, "keys", {"groq": ["gsk_first_secret"]})
    pool.db.execute("DELETE FROM keys WHERE api_key=?", ("gsk_second_secret",))
    pool.db.commit()
    seen = {"n": 0}

    def flaky(provider, api_key, body, begin=None, on_chunk=None):
        seen["n"] += 1
        if seen["n"] == 1:
            raise ConnectionError("Remote end closed connection without response")
        return 200, json.dumps({"choices": [{"message": {"content": "да"}}]})

    monkeypatch.setattr(pool_server, "call_upstream", flaky)
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        first = complete(client, pool.base, token)
        assert first.status_code == 502, "the seat is told the truth about the dead turn"
        dead = pool.db.execute("SELECT cooldown_until, used_tokens FROM keys WHERE id=1").fetchone()
        left = dead["cooldown_until"] - time.time()
        assert dead["used_tokens"] == 0, "a request that never happened costs nothing"
        assert left < 10, f"the key is still leased for {left:.0f} s of LEASE_SECONDS"
        free_the_keys(pool)                       # the one-second rest, without sleeping
        second = complete(client, pool.base, token)
        assert second.status_code == 200, f"the next seat paid for the crash: {second.text}"


def test_a_stream_pays_for_the_tokens_it_declared(pool, monkeypatch):
    """`usage` is in the stream; the estimate was built from `{}` because of it.

    Measured: an answer declaring 999 999 tokens charged the 214 bytes that carried
    it, and the ceiling never moved.
    """
    monkeypatch.setattr(pool_server, "call_upstream", stream_upstream(STREAM_FRAMES))
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, stream=True)
        assert answer.status_code == 200, answer.text
        row = pool.db.execute("SELECT used_tokens FROM keys ORDER BY id LIMIT 1").fetchone()
    assert row["used_tokens"] >= 999999, \
        f"the stream declared 999999 and the key was charged {row['used_tokens']}"


def test_a_stream_framed_weirdly_still_pays(pool, monkeypatch):
    """The same answer with no `data:` prefix on any line.

    The frames we forward are the only thing the old estimate saw, so an upstream
    that frames its chunks differently — including one a seat aims at on purpose
    with `pool_provider` — billed a whole long turn as nothing at all.
    """
    unframed = [line[6:] if line.startswith("data: ") else line for line in STREAM_FRAMES]
    monkeypatch.setattr(pool_server, "call_upstream", stream_upstream(unframed))
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, stream=True)
        assert answer.status_code == 200, answer.text
        row = pool.db.execute("SELECT used_tokens FROM keys ORDER BY id LIMIT 1").fetchone()
    assert row["used_tokens"] >= 999999, \
        f"the framing hid the answer: charged {row['used_tokens']} of 999999"


def test_a_forwarded_stream_separates_its_events(pool, monkeypatch):
    """An SSE event is a `data:` line AND a blank line.

    The pool used to write the line and one newline, so every frame of the answer
    arrived to the reader as one event — `}{` where one object was expected — and a
    client that follows the spec (which the BeeCode client now does) reported a torn
    stream instead of an answer.
    """
    monkeypatch.setattr(pool_server, "call_upstream", stream_upstream(STREAM_FRAMES))
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        answer = complete(client, pool.base, token, stream=True)
    assert answer.status_code == 200, answer.text
    events = [event for event in answer.text.split("\n\n") if event.strip()]
    assert events == STREAM_FRAMES, answer.text
    first = json.loads(events[0][len("data: "):])
    assert first["choices"][0]["delta"]["content"] == "раз"


def test_a_stream_that_names_only_its_parts_still_bills_them():
    """Providers omit `total_tokens` far more often than they omit its halves, and
    prompt plus completion is what the account was billed for."""
    payload, said = pool_server.stream_accounting(
        'data: {"choices":[{"delta":{"content":"раз"}}]}\n\n'
        'data: {"usage":{"prompt_tokens":4000,"completion_tokens":6000}}\n\n'
        'data: [DONE]\n\n')
    assert said == "раз"
    assert payload["usage"]["total_tokens"] == 10000
    assert pool_server.estimate_tokens(payload, said) == 10000


def test_a_seat_cannot_spend_past_its_own_token_budget(pool):
    """`tokens_limit` was printed by `/v1/seat` and never read by the path that
    spends it, so a seat walked 30M tokens against a 400k limit without a word."""
    pool.answers[:] = [(200, json.dumps({"choices": [{"message": {"content": "да"}}],
                                         "usage": {"total_tokens": 5_000_000}}))]
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        assert complete(client, pool.base, token).status_code == 200
        over = complete(client, pool.base, token)
        assert over.status_code == 429, \
            f"the seat blew its budget and nothing said so: {over.text}"
        body = over.json()
        report = client.get(pool.base + "/v1/seat",
                            headers={"Authorization": f"Bearer {token}"}).json()
    assert "token" in body["error"] and "budget" in body["error"], body
    assert body["resets_in_seconds"] > 60, "the wait is until midnight, not a retry"
    assert int(over.headers.get("Retry-After", 0)) > 60
    assert len(pool.calls) == 1, "an over-budget seat must not knock on the provider again"
    assert report["tokens"] >= report["tokens_limit"], "the number the seat can already see"


def test_a_lowered_ceiling_is_applied_to_every_key(pool, monkeypatch):
    """The one edit an operator makes in a hurry is the number going down, because a
    key is burning. It used to do nothing: the UPDATE filled only rows at zero."""
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "400000")
    pool_server.sync_keys()
    assert limits_of(pool) == [400000, 400000]
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "100000")
    pool_server.sync_keys()
    assert limits_of(pool) == [100000, 100000], "the configured value is the authority"


def test_a_ceiling_typed_the_human_way_does_not_stop_the_box(pool, monkeypatch):
    """"400 000" is one number a person means, and `int()` of it killed the start
    thread: measured against a real boot, that value means "pool did not start"."""
    for typed, meant in (("400 000", 400000), ("400,000", 400000), ("400_000", 400000),
                         (" 400000 ", 400000)):
        monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", typed)
        pool_server.sync_keys()
        assert limits_of(pool) == [meant, meant], typed
        assert not any("WARNING" in note
                       for note in pool_server.ceiling_config_notes()), typed


def test_a_ceiling_that_is_not_a_number_at_all_is_refused_loudly(pool, monkeypatch):
    """No ceiling in disguise: the value is named, ignored, and the box still boots."""
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "много")
    pool_server.sync_keys()                       # must not raise, then or at boot
    assert limits_of(pool) == [0, 0], "a nonsense value does not become a nonsense ceiling"
    notes = pool_server.ceiling_config_notes()
    assert any("WARNING" in note and "IGNORED" in note for note in notes), notes
    assert not any("много" in note for note in notes), "not echoed where the console is cp1251"
    logged = pool.db.execute("SELECT note FROM events WHERE kind='ceiling-refused'").fetchall()
    assert logged, "the operator's log says what the terminal said"


def test_the_startup_banner_says_what_the_ceiling_really_is(pool, monkeypatch):
    """A pool with no ceiling is a pool that loses a key to the provider's own wall,
    and every operator believed theirs had one because the deploy file names 400000.
    Both directions have to be on the terminal before the port opens."""
    monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", "400000")
    notes = pool_server.startup_notes()
    assert any("400000" in note and "ceiling" in note for note in notes), notes
    for unset in (None, "", "0"):
        if unset is None:
            monkeypatch.delenv("BEECODE_POOL_KEY_CEILING", raising=False)
        else:
            monkeypatch.setenv("BEECODE_POOL_KEY_CEILING", unset)
        notes = pool_server.startup_notes()
        assert any("NONE" in note and "ceiling" in note for note in notes), (unset, notes)
    assert notes[0] == pool_server.admin_config_notes()[0], "the operator gate is still first"


def test_an_unapproved_seat_gets_neither_the_catalogue_nor_the_report(pool, monkeypatch):
    """`/v1/models` and `/v1/seat` answer on a seat token alone, and that is a choice
    (see `_bearer_seat`) — but it cannot mean a seat the owner has not said yes to
    reads the pool's inventory and its own remaining budget to plan with."""
    monkeypatch.setenv("BEECODE_POOL_APPROVAL", "1")
    monkeypatch.setattr(pool_server, "list_upstream_models", lambda p, k: ["llama-3.3-70b"])
    for name, value in (("at", 0.0), ("ids", []), ("note", "")):
        monkeypatch.setitem(pool_server._models_cache, name, value)
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        headers = {"Authorization": f"Bearer {token}"}
        assert client.get(pool.base + "/v1/models", headers=headers).status_code == 403
        assert client.get(pool.base + "/v1/seat", headers=headers).status_code == 403
        client.post(pool.base + "/v1/admin/approve", json={"token": token},
                    headers={"X-Admin": "admin-secret"})
        assert client.get(pool.base + "/v1/models", headers=headers).status_code == 200
        assert client.get(pool.base + "/v1/seat", headers=headers).status_code == 200


def test_a_seat_token_alone_cannot_make_the_pool_dial_the_accounts(pool, monkeypatch):
    """The list route is the one place an unsigned caller made this box knock on the
    operator's accounts: a provider that would not list left the cache cold forever
    and every retry dialled every key again, with a 20 s timeout each. Genuinely
    harmless means it can be hammered: one dial, nothing leased, nothing spent."""
    dials = []

    def silent(provider, api_key):
        dials.append(provider)
        return []

    monkeypatch.setattr(pool_server, "list_upstream_models", silent)
    for name, value in (("at", 0.0), ("ids", []), ("note", "")):
        monkeypatch.setitem(pool_server._models_cache, name, value)
    with httpx.Client(timeout=CLIENT_TIMEOUT) as client:
        token = enroll(client, pool.base)
        headers = {"Authorization": f"Bearer {token}"}
        answers = [client.get(pool.base + "/v1/models", headers=headers) for _ in range(5)]
        seats = [client.get(pool.base + "/v1/seat", headers=headers) for _ in range(5)]
        assert all(a.status_code == 502 for a in answers), [a.status_code for a in answers]
        assert all(s.status_code == 200 for s in seats)
        seat = client.get(pool.base + "/v1/seat", headers=headers).json()
        keys = pool.db.execute("SELECT used_tokens, cooldown_until FROM keys").fetchall()
    assert len(dials) == 1, f"ten requests dialled the accounts {len(dials)} times"
    assert all(r["used_tokens"] == 0 and r["cooldown_until"] <= time.time() for r in keys), \
        "no key leased and no token spent"
    assert seat["requests"] == 0 and seat["tokens"] == 0, "the seat's own budget untouched"
    assert "gsk_" not in json.dumps([a.json() for a in answers] + [s.json() for s in seats])
