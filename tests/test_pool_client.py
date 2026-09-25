"""The pool client, against a pool that is sometimes asleep.

Nothing here needs the server file: these are the client's own promises — that a
sleeping free instance gets one patient wait, that an answer is never asked twice,
and that a pool which never wakes says why in words a user can act on.

Every request in this file is answered by the stub `Wire` below. Nothing here has
ever spoken to a deployed pool: `httpx.AsyncClient` and `httpx.Client` are replaced
before a provider is constructed, so a request cannot leave the process and no
seat's quota is spent by a test run.
"""
import asyncio
import json
from contextlib import asynccontextmanager

import httpx
import pytest

from beeagent.i18n import get_lang, set_lang
from beeagent.providers import pool as pool_mod
from beeagent.providers.base import ProviderStreamError
from beeagent.providers.pool import PoolError


@pytest.fixture(autouse=True)
def install_key_in_tmp(tmp_path, monkeypatch):
    """Signing must not create a key in the home directory of whoever runs this."""
    monkeypatch.setenv("BEECODE_POOL_KEY_FILE", str(tmp_path / "pool-key.json"))


@pytest.fixture(autouse=True)
def english():
    """The assertions quote the English wording; the pair is checked separately."""
    previous = get_lang()
    set_lang("en")
    yield
    set_lang(previous)


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


# --- a wire that answers, and remembers what we sent -----------------------
#
# The frames below are written the way the wire writes them: an SSE event is a
# `data:` line plus a blank line, and `aiter_lines()` hands over lines, not
# events. Every case here is one the pool has actually been measured producing.

def frame(delta) -> str:
    """One OpenAI-shaped delta frame, exactly as the pool frames it."""
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": delta}]})


def content_frame(text) -> str:
    return frame({"content": text})


class Wire:
    """The whole httpx surface a client uses, replaying a script and logging calls.

    `respond(kind, url, kw)` returns a `Reply`, a `Reply`-like object or the
    exception the connection raised. Nothing is ever sent anywhere.
    """

    def __init__(self, respond):
        self.respond = respond
        self.requests = []
        self.clients = []

    def ask(self, kind, method, url, kw, client_kw=None):
        self.requests.append({"kind": kind, "method": method, "url": url, "kw": kw,
                              "client": client_kw or {}})
        out = self.respond(kind, url, kw)
        if isinstance(out, BaseException):
            raise out
        return out

    def install(self, monkeypatch):
        monkeypatch.setattr(pool_mod.httpx, "AsyncClient",
                            lambda *a, **kw: _Async(self, *a, **kw))
        monkeypatch.setattr(pool_mod.httpx, "Client",
                            lambda *a, **kw: _Sync(self, *a, **kw))
        return self

    def urls(self, kind=None):
        return [r["url"] for r in self.requests if kind is None or r["kind"] == kind]

    def last(self, needle=""):
        for entry in reversed(self.requests):
            if needle in entry["url"]:
                return entry
        raise AssertionError(f"no request to {needle!r}: {self.urls()}")

    def headers(self, needle=""):
        return self.last(needle)["kw"].get("headers")


class Reply:
    """A response with the exact lines the endpoint put on the wire."""

    def __init__(self, lines=None, status=200, body="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._lines = list(lines) if lines is not None else []
        self.text = body if body else "".join(line + "\n" for line in self._lines)
        self.is_stream_consumed = False
        self.request = httpx.Request("POST", "http://pool.invalid")

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        self.is_stream_consumed = True
        return self.text.encode()

    def json(self):
        body = json.loads(self.text)
        if not isinstance(body, dict):
            raise ValueError("not an object")
        return body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"Client error '{self.status_code} for url",
                                        request=self.request, response=self)


class _StreamCM:
    def __init__(self, wire, method, url, kw, client_kw=None):
        self.wire, self.method, self.url = wire, method, url
        self.kw, self.client_kw = kw, client_kw or {}

    async def __aenter__(self):
        return self.wire.ask("stream", self.method, self.url, self.kw, self.client_kw)

    async def __aexit__(self, *exc):
        return False


class _Async:
    def __init__(self, wire, *a, **kw):
        self.wire, self.client_kw = wire, kw
        wire.clients.append(kw)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, **kw):
        return self.wire.ask("post", "POST", url, kw, self.client_kw)

    async def get(self, url, **kw):
        return self.wire.ask("get", "GET", url, kw, self.client_kw)

    def stream(self, method, url, **kw):
        return _StreamCM(self.wire, method, url, kw, self.client_kw)


class _Sync:
    def __init__(self, wire, *a, **kw):
        self.wire, self.client_kw = wire, kw
        wire.clients.append(kw)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kw):
        return self.wire.ask("post", "POST", url, kw, self.client_kw)

    def get(self, url, **kw):
        return self.wire.ask("get", "GET", url, kw, self.client_kw)


def collect(agen):
    async def drain():
        return [pair async for pair in agen]

    return asyncio.run(drain())


def collect_with_error(agen):
    """The pieces that reached the screen, and the error that followed them."""
    async def drain():
        pieces, error = [], None
        try:
            async for pair in agen:
                pieces.append(pair)
        except Exception as e:            # noqa: BLE001 — the test names the type
            error = e
        return pieces, error

    return asyncio.run(drain())


def seen(pieces) -> str:
    return "".join(text for _, text in pieces)


ANSWER_THEN_TORN = [content_frame("Hel"), "", content_frame("lo the"), "",
                    'data: {"choices": [{"delta": {"content": "re"}}', ""]


# --- the answer is the whole answer, or it is an error ---------------------

def test_a_torn_frame_is_reported_and_not_left_as_the_answer(monkeypatch, no_wait):
    """Measured: the split event made the answer read "Hello the".

    The third chunk was dropped and the generator ended without an error, so one
    POST was charged for a half answer the user read as complete.
    """
    Wire(lambda k, u, kw: Reply(lines=ANSWER_THEN_TORN)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert seen(pieces) == "Hello the", "the text that did arrive is what got shown"
    assert isinstance(error, ProviderStreamError), f"the half answer ended quietly: {error!r}"
    assert "cannot be read" in str(error) and "not the whole answer" in str(error)


def test_the_pool_failure_is_still_a_PoolError_for_every_caller(monkeypatch, no_wait):
    """`except PoolError` is written down elsewhere; sharing the reader may not
    quietly change the type a caller has to catch."""
    Wire(lambda k, u, kw: Reply(lines=ANSWER_THEN_TORN)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    with pytest.raises(PoolError):
        collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))


def test_one_event_spread_over_two_data_lines_is_the_whole_answer(monkeypatch, no_wait):
    """The spec allows it and one gateway does it: two `data:` lines, one payload.

    Read line-by-line both halves are unparseable, and the pool ended up saying
    "the pool streamed nothing" about an answer that was there.
    """
    frames = ['data: {"choices": [{"delta": {"content":',
              'data:  "the whole answer"}}]}', "", "data: [DONE]"]
    Wire(lambda k, u, kw: Reply(lines=frames)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    assert collect(provider.chat_stream([{"role": "user", "content": "х"}], "m")) == \
        [("content", "the whole answer")]


def test_the_frames_the_pool_actually_sends_have_no_blank_line_between_them(
        monkeypatch, no_wait):
    """`pool_server._drain` forwards the upstream's `data:` lines and drops the
    blank line after each, which is the shape a live seat is billed for.

    A reader that insists on the separator glues the frames into one payload that
    parses as nothing, so every streamed answer of a working pool would come back
    as a broken one.
    """
    frames = [json.dumps({"choices": [{"delta": {"reasoning_content": "думаю"}}]}),
              json.dumps({"choices": [{"delta": {"content": "раз"}}]})]
    lines = ["data: " + frames[0], "data: " + frames[1], "data: [DONE]"]
    Wire(lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    assert collect(provider.chat_stream([{"role": "user", "content": "х"}], "m")) == \
        [("reasoning", "думаю"), ("content", "раз")]


@pytest.mark.parametrize("marker", ["data:  [DONE]", "[DONE]", "data: [DONE]", "data:[DONE]",
                                    'data: "[DONE]"'])
def test_every_spelling_of_done_ends_the_answer_there(monkeypatch, no_wait, marker):
    """`data:  [DONE]` with two spaces was not a match, so the stream kept reading.

    Whatever the endpoint wrote after the marker — retries, keep-alives, the next
    request's answer — landed in the user's answer.
    """
    frames = [content_frame("real answer"), "", marker, "",
              content_frame(" <<POST-DONE GARBAGE>>"), ""]
    Wire(lambda k, u, kw: Reply(lines=frames)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert seen(pieces) == "real answer"
    assert error is None, "a clean [DONE] is not a failure"


def test_reasoning_and_content_keep_their_channels_apart(monkeypatch, no_wait):
    frames = [frame({"reasoning_content": "думаю"}), "", content_frame("раз"), "",
              "data: [DONE]"]
    Wire(lambda k, u, kw: Reply(lines=frames)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    assert collect(provider.chat_stream([{"role": "user", "content": "х"}], "m")) == \
        [("reasoning", "думаю"), ("content", "раз")]


def test_an_error_frame_after_text_is_the_reason_and_not_the_truncated_text(
        monkeypatch, no_wait):
    frames = [content_frame("half an "), "",
              "data: " + json.dumps({"error": {"message": "model is overloaded"}}), ""]
    Wire(lambda k, u, kw: Reply(lines=frames)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert seen(pieces) == "half an "
    assert error is not None and "model is overloaded" in str(error), \
        f"the reason was thrown away: {error!r}"


def test_a_prefix_less_error_frame_still_says_why(monkeypatch, no_wait):
    """Ollama-shaped gateways behind the pool answer `{"error": …}` with no `data:`."""
    Wire(lambda k, u, kw: Reply(lines=['{"error": "no such model: llama99"}', ""]))\
        .install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    with pytest.raises(PoolError) as raised:
        collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))
    assert "no such model: llama99" in str(raised.value)


# --- what a silent or refusing pool is called ------------------------------

def test_a_silent_pool_says_so_instead_of_leaking_a_read_timeout(monkeypatch, no_wait):
    """`httpx.ReadTimeout("")` has no message: the user read an empty error."""
    wire = Wire(lambda k, u, kw: httpx.ReadTimeout(""))
    wire.install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token",
                                     idle_timeout=42.0)

    for label, call in (("chat", lambda: asyncio.run(
                             provider.chat([{"role": "user", "content": "х"}], "m"))),
                        ("chat_stream", lambda: collect(
                             provider.chat_stream([{"role": "user", "content": "х"}], "m")))):
        with pytest.raises(PoolError) as raised:
            call()
        text = str(raised.value).strip()
        assert text, f"{label} reported an empty error"
        assert "ReadTimeout" not in text and "42" in text, f"{label}: {text!r}"


def test_a_stream_dropped_mid_answer_is_not_glued_to_the_next_one(monkeypatch, no_wait):
    """Text was already on screen; a second request would print and pay twice."""
    asks = []

    def respond(kind, url, kw):
        asks.append(url)
        return Reply(lines=[content_frame("раз"), ""])

    Wire(respond).install(monkeypatch)

    async def drop_after_the_answer(self):
        for line in self._lines:
            yield line
        raise httpx.ReadTimeout("gone quiet")

    monkeypatch.setattr(Reply, "aiter_lines", drop_after_the_answer)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert seen(pieces) == "раз", "the text that arrived is what the user saw"
    assert isinstance(error, PoolError), f"a dropped stream ended quietly: {error!r}"
    assert "раз" not in str(error), "the error must not repeat the answer"
    assert len(asks) == 1, f"the answer was asked twice: {asks}"


# --- the model list says where it came from --------------------------------

def test_no_seat_a_revoked_seat_and_a_live_list_are_three_different_states(monkeypatch):
    """A user whose seat was revoked saw a healthy model menu.

    The offline list stays on purpose — crax's Cloudflare blocks some networks —
    but it may never look like the answer of a live pool.
    """
    # (a) no seat at all
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="")
    with pytest.raises(PoolError):
        provider.discover_models()
    assert provider.model_list_state == "no-seat"
    assert "/pool enroll" in provider.model_list_note()

    # (b) a seat the pool refused
    refused = Wire(lambda k, u, kw: Reply(status=401,
                                          body=json.dumps({"error": "this seat expired yesterday"})))
    refused.install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")
    with pytest.raises(PoolError):
        provider.discover_models()
    assert provider.model_list_state == "seat-refused"
    note = provider.model_list_note()
    assert "this seat expired yesterday" in note, f"the reason is gone: {note!r}"
    assert "ships" in note or "поставля" in note, f"not marked as an offline list: {note!r}"

    # (c) a pool that never answered
    silent = Wire(lambda k, u, kw: httpx.ConnectError("connection refused"))
    silent.install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")
    with pytest.raises(PoolError):
        provider.discover_models()
    assert provider.model_list_state == "unreachable"

    # (d) the live list, which is the only state that is not a fallback
    live = Wire(lambda k, u, kw: Reply(status=200, body=json.dumps(
        {"data": [{"id": "model-from-the-pool"}]})))
    live.install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")
    assert provider.discover_models() == ["model-from-the-pool"]
    assert provider.model_list_state == "live"


def test_the_fallback_list_is_still_the_shipped_one_and_is_never_asked_silently(
        monkeypatch):
    """`/models` still has to show something when the box is asleep."""
    Wire(lambda k, u, kw: Reply(status=503, body=json.dumps({"error": "no keys"})))\
        .install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    with pytest.raises(PoolError):
        provider.discover_models()

    assert provider.models == list(pool_mod.PoolProvider.models)
    assert provider.model_list_state != "live"
    assert provider.serves("qwen3-coder-480b") is True
    assert provider.serves("a-model-this-install-never-heard-of") is False


# --- routing must not be told a lie about the catalogue --------------------

def test_models_reports_what_the_endpoint_answered_not_what_shipped(monkeypatch):
    """The picker offered the live list while routing checked the compile-time one.

    Picking `llama-4-scout` therefore sent `llama-3.1-8b-instant` and rewrote
    `config.model` — the request the user asked for never left the machine.
    """
    live = ["llama-4-scout", "qwen3-32b"]
    Wire(lambda k, u, kw: Reply(status=200,
                                body=json.dumps({"data": [{"id": m} for m in live]})))\
        .install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    assert provider.discover_models() == live
    assert provider.models == live, "`models` still answers with the offline list"
    assert provider.serves("llama-4-scout") is True
    assert provider.serves("glm-5.2") is False, "a name the live pool does not carry"
    assert "model-from-the-pool" not in provider.models


# --- /pool status speaks for the seat, or says it cannot -------------------

def test_pool_status_sends_the_seat_token_and_reports_the_seat(monkeypatch):
    def respond(kind, url, kw):
        if url.endswith("/healthz"):
            return Reply(status=200, body=json.dumps({"ok": True}))
        return Reply(status=200, body=json.dumps({"requests": 3, "requests_limit": 200}))

    wire = Wire(respond)
    wire.install(monkeypatch)

    health = pool_mod.pool_status("http://pool.invalid", "seat-token")

    seat = wire.last("/v1/seat")
    assert seat["kw"]["headers"]["Authorization"] == "Bearer seat-token", \
        "the status spoke about the server while sounding like it spoke about the seat"
    assert seat["kw"]["timeout"] <= pool_mod.SEAT_WAIT, \
        "`/pool status` runs on the prompt's thread: it must not wait out a cold start twice"
    assert health["seat"]["requests_limit"] == 200
    assert health["seat"]["ok"] is True


def test_pool_status_says_the_seat_is_not_known_even_when_the_server_is_alive(monkeypatch):
    def respond(kind, url, kw):
        if url.endswith("/healthz"):
            return Reply(status=200, body=json.dumps({"ok": True}))
        return Reply(status=401, body=json.dumps({"error": "no seat — /pool enroll first"}))

    Wire(respond).install(monkeypatch)
    health = pool_mod.pool_status("http://pool.invalid", "revoked-token")

    assert health["ok"] is True, "the server really is up"
    assert health["seat"]["ok"] is False
    assert "/pool enroll" in health["seat"]["error"]


def test_pool_status_without_a_seat_says_so_without_asking(monkeypatch):
    wire = Wire(lambda k, u, kw: Reply(status=200, body=json.dumps({"ok": True})))
    wire.install(monkeypatch)

    health = pool_mod.pool_status("http://pool.invalid", "")

    assert health["seat"]["ok"] is False
    assert "no seat" in health["seat"]["error"]
    assert wire.urls("get") == [url for url in wire.urls("get") if url.endswith("/healthz")]


# --- the timeouts the agent explains are the timeouts the client uses ------

def test_the_stream_waits_as_long_as_the_agent_says_it_may(monkeypatch, no_wait):
    """`stream_idle_timeout` is the budget the agent narrates a stall with.

    A read timeout shorter than it kills a slow model first, and the user reads a
    bare ReadTimeout as a broken pool.
    """
    Wire(lambda k, u, kw: Reply(lines=[content_frame("x"), "", "data: [DONE]"]))\
        .install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token",
                                     idle_timeout=123.0)

    collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    timeout = provider._timeout()
    assert timeout.read == 123.0
    assert timeout.connect <= 10.0


def test_a_pool_that_wants_no_config_at_all_still_waits_for_the_agent_default(
        monkeypatch, no_wait):
    Wire(lambda k, u, kw: Reply(lines=[content_frame("x"), "", "data: [DONE]"]))\
        .install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")

    collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert provider._timeout().read >= 60.0, "a pool built without a config is not hurried"


# --- bilingual -------------------------------------------------------------

def test_the_new_honesties_have_a_russian_wording_too(monkeypatch, no_wait):
    """Every user-facing string here is a pair; an untranslated one is a bug."""
    Wire(lambda k, u, kw: Reply(lines=ANSWER_THEN_TORN)).install(monkeypatch)
    provider = pool_mod.PoolProvider(url="http://pool.invalid", token="seat-token")
    previous = get_lang()
    set_lang("ru")
    try:
        _, torn = collect_with_error(provider.chat_stream([{"role": "user", "content": "х"}], "m"))
        seat = pool_mod.PoolProvider(url="http://pool.invalid", token="")
        with pytest.raises(PoolError):
            seat.discover_models()
        note = seat.model_list_note()
    finally:
        set_lang(previous)

    assert "не читается" in str(torn) and "не весь ответ" in str(torn)
    assert "места" in note or "/pool enroll" in note
