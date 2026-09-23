"""The pool client, against a pool that is sometimes asleep.

Nothing here needs the server file: these are the client's own promises — that a
sleeping free instance gets one patient wait, that an answer is never asked twice,
and that a pool which never wakes says why in words a user can act on.
"""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from beeagent.providers import pool as pool_mod
from beeagent.providers.pool import PoolError


@pytest.fixture(autouse=True)
def install_key_in_tmp(tmp_path, monkeypatch):
    """Signing must not create a key in the home directory of whoever runs this."""
    monkeypatch.setenv("BEECODE_POOL_KEY_FILE", str(tmp_path / "pool-key.json"))


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {
            "choices": [{"message": {"content": "жужж"}}]}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class Flaky:
    """A stand-in for httpx.AsyncClient that refuses to connect on cue."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _connect(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise httpx.ConnectError("connection refused")

    async def post(self, url, content=None, headers=None):
        self._connect()
        assert content, "the body is sent as bytes so the signature covers it"
        json.loads(content)
        return FakeResponse()

    @asynccontextmanager
    async def stream(self, method, url, content=None, headers=None):
        assert json.loads(content)["stream"] is True
        self._connect()
        outer = self

        class Streaming(FakeResponse):
            async def aread(self):
                return b""

            async def aiter_lines(self):
                for line in (f'data: {{"choices": [{{"delta": {{"content": "раз"}}}}]}}',
                             "data: [DONE]"):
                    yield line

        yield Streaming()
        assert outer.calls >= 1


@pytest.fixture()
def no_wait(monkeypatch):
    monkeypatch.setattr(pool_mod, "COLD_START_WAIT", 0)


def make(flaky, monkeypatch):
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", flaky)
    return pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")


def test_a_sleeping_pool_gets_one_patient_wait(no_wait, monkeypatch):
    """A free Render instance sleeps after fifteen idle minutes; the request that
    wakes it should not be the one that reports a broken pool."""
    flaky = Flaky(failures=1)
    provider = make(flaky, monkeypatch)

    answer = asyncio.run(provider.chat([{"role": "user", "content": "привет"}], "m"))

    assert answer == "жужж"
    assert flaky.calls == 2, "one wake-up, not a retry storm"


def test_a_pool_that_never_wakes_says_it_might_be_asleep(no_wait, monkeypatch):
    flaky = Flaky(failures=5)
    provider = make(flaky, monkeypatch)

    with pytest.raises(PoolError) as raised:
        asyncio.run(provider.chat([{"role": "user", "content": "привет"}], "m"))

    assert "wake up" in str(raised.value) or "пробуждени" in str(raised.value)
    assert flaky.calls == 2, "exactly one retry, then the honest error"


def test_a_stream_is_asked_once_even_when_the_first_attempt_refused(no_wait, monkeypatch):
    flaky = Flaky(failures=1)
    provider = make(flaky, monkeypatch)

    async def collect():
        return [pair async for pair in provider.chat_stream([{"role": "user", "content": "х"}], "m")]

    assert asyncio.run(collect()) == [("content", "раз")]
    assert flaky.calls == 2


def test_a_stream_that_already_started_is_not_repeated(no_wait, monkeypatch):
    """Once fragments are on screen, asking again would print the answer twice and
    pay for it twice — so only a connection that never opened is retried."""
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")
    attempts = []

    async def half_then_fail(messages, model, token):
        attempts.append(1)
        yield ("content", "раз")
        raise httpx.ConnectError("dropped")

    monkeypatch.setattr(provider, "_stream", half_then_fail)

    async def collect():
        return [pair async for pair in provider.chat_stream([{"role": "user", "content": "х"}], "m")]

    with pytest.raises(PoolError):
        asyncio.run(collect())
    assert len(attempts) == 1, "the first fragment was already shown"


def test_enrolment_is_given_long_enough_to_wake_the_pool():
    """The one call without a retry has to outlast a cold start on its own: a
    stranger's first `/pool enroll` is exactly the request that finds the pool
    asleep, and a timeout there reads as a broken server."""
    assert pool_mod.ENROLL_TIMEOUT > pool_mod.COLD_START_WAIT
