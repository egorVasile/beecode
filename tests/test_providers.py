import asyncio

import pytest

from beeagent.providers.base import BaseProvider
from beeagent.providers.registry import ProviderRegistry

class DummyProvider(BaseProvider):
    name = "dummy"
    models = ["dummy-model"]
    
    async def chat(self, messages, model="dummy-model", stream=False):
        return "dummy response"

def test_provider_registry():
    registry = ProviderRegistry()
    registry.register(DummyProvider())
    assert "dummy" in registry.list_names()

def test_provider_get():
    registry = ProviderRegistry()
    registry.register(DummyProvider())
    p = registry.get("dummy")
    assert p is not None
    assert p.name == "dummy"

def test_provider_get_missing():
    registry = ProviderRegistry()
    assert registry.get("nonexistent") is None

def test_provider_select_refuses_unknown_names():
    registry = ProviderRegistry()
    registry.register(DummyProvider())
    try:
        registry.select("nonexistent")
        raise AssertionError("unknown provider must not silently fall back")
    except RuntimeError as e:
        assert "nonexistent" in str(e) and "dummy" in str(e)

def test_provider_select_returns_preferred():
    registry = ProviderRegistry()
    
    class OtherProvider(BaseProvider):
        name = "other"
        models = ["other-model"]
        async def chat(self, messages, model="", stream=False):
            return "other"
    
    registry.register(DummyProvider())
    registry.register(OtherProvider())
    p = registry.select("other")
    assert p.name == "other"


from beeagent.providers.g4f_provider import G4fProvider

def test_g4f_provider_init():
    p = G4fProvider()
    assert p.name == "g4f"
    assert len(p.models) > 0

def test_g4f_provider_has_models():
    p = G4fProvider()
    # the two ids the pinned providers answer with their own names
    assert "command-a-03-2025" in p.models
    assert "default" in p.models

def test_g4f_provider_is_base():
    from beeagent.providers.base import BaseProvider
    p = G4fProvider()
    assert isinstance(p, BaseProvider)


def test_g4f_curated_models_are_only_what_the_pin_serves():
    """Every quick pick has to be reachable, or the picker offers a dead choice.

    The list used to name glm, gemini, claude, gpt-4.x, kimi, qwen, llama,
    deepseek and grok, all of which auto-routing served and nothing else can
    reach now that the fallback is gone.
    """
    from beeagent.providers.g4f_provider import _pinned_models

    p = G4fProvider()
    assert p.models, "a provider with no reachable model is not a provider"
    advertised = set(_pinned_models())
    unreachable = [m for m in p.models if m not in advertised]
    assert not unreachable, f"no pinned provider advertises {unreachable}"
    for dead in ("gpt-4o", "gemini-2.5-pro", "glm-4.7-flash", "deepseek-chat"):
        assert dead not in p.models, f"{dead} answers only through auto-routing"
    # the route measured to carry a 32k prompt keyless leads the list
    assert p.models[0] == "command-a-03-2025"


def test_every_keyless_provider_still_resolves():
    """The fallback list rotted once in silence.

    It named Free2GPT, Blackbox and DDG — deleted upstream — so `_keyless_providers`
    returned two classes and auto-routing carried on picking keyed endpoints.
    """
    import g4f.Provider as P

    from beeagent.providers.g4f_provider import KEYLESS_PROVIDERS

    missing = []
    for name in KEYLESS_PROVIDERS:
        try:
            getattr(P, name)
        except Exception:
            missing.append(name)
    assert not missing, f"g4f no longer ships {missing}; drop them from KEYLESS_PROVIDERS"


def test_a_stream_g4f_returns_unwrapped_survives_the_provider():
    """Auto-routing hands back an async generator, not a coroutine.

    Awaiting that raised "object async_generator can't be used in 'await'
    expression" for every streamed answer, and the provider turned it into
    "g4f streamed nothing" — so no token ever reached the terminal.
    """
    import asyncio

    from beeagent.providers.g4f_provider import _await_or_keep

    async def as_generator():
        yield "chunk"

    async def as_coroutine():
        return "value"

    async def main():
        stream = as_generator()
        try:
            assert await _await_or_keep(stream) is stream, "a stream must not be awaited"
            assert await _await_or_keep(as_coroutine()) == "value"
        finally:
            await stream.aclose()

    asyncio.run(main())


def test_discover_models_lists_only_what_a_pinned_provider_serves():
    """The old contract was "every model of every working provider" — 600+ names.

    A request now leaves the machine to LLM7 or CohereForAI and nowhere else, so
    a name those two do not advertise is a dead choice in the picker, and a big
    catalogue number is the bug rather than the guarantee. g4f's own lists still
    overstate the space behind them, so the ids measured silent stay out too.
    """
    from beeagent.providers.g4f_provider import MEASURED_SILENT, _pinned_models

    G4fProvider._discovered = None
    catalog = G4fProvider.discover_models()
    advertised = set(_pinned_models())

    assert catalog[:len(G4fProvider.models)] == G4fProvider.models, "curated picks lead"
    assert len(catalog) == len(set(catalog)), "no duplicates"
    extra = set(catalog) - advertised
    assert not extra, f"no pinned provider advertises {sorted(extra)}"
    assert not set(catalog) & set(MEASURED_SILENT), "an id that never answered is listed"
    assert len(catalog) == len(advertised - set(MEASURED_SILENT))
    for dead in ("gpt-4o", "llama-3.1-70b", "glm-4.7-flash"):
        assert dead not in catalog, f"{dead} was reachable only through auto-routing"


def test_discover_models_never_asks_the_network(monkeypatch):
    """`/models` opens a picker while you type; the scan stays a package read."""
    import socket

    def no_way(*args, **kwargs):
        raise AssertionError("discover_models reached the network")

    monkeypatch.setattr(socket, "socket", no_way)
    monkeypatch.setattr(socket, "create_connection", no_way)
    G4fProvider._discovered = None
    try:
        assert G4fProvider.discover_models()
    finally:
        G4fProvider._discovered = None


def test_available_models_is_the_pinned_catalog():
    from beeagent.ui.commands import ReplContext, available_models
    from beeagent.config.schema import BeeConfig
    from beeagent.core.session import Session

    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    models = available_models(ctx)
    assert models == G4fProvider.discover_models()
    assert "command-a-03-2025" in models
    assert "gpt-4" not in models, "nothing on the pin answers that name"


def test_served_by_credits_only_the_pinned_providers():
    """The `/models` table prints `upstream_map()` under "served by".

    Scanning all of g4f for it credited BlackboxPro and Cloudflare with models
    this provider can no longer ask them for — a second listing that disagreed
    with the first.
    """
    from beeagent.providers.g4f_provider import KEYLESS_PROVIDERS

    G4fProvider._upstream_map = None
    mapping = G4fProvider.upstream_map()
    named = {name for providers in mapping.values() for name in providers}
    assert named <= set(KEYLESS_PROVIDERS), f"the table names providers we never call: {named}"
    for model in G4fProvider.discover_models():
        assert mapping.get(model), f"{model} is listed as if nothing served it"


def test_by_window_leads_with_the_biggest_context():
    from beeagent.core import windows

    ordered = G4fProvider.by_window(["gpt-4", "gemini-2.5-pro", "glm-4.7-flash"])
    assert ordered[0] == "gemini-2.5-pro", "a 1M-window name beats an 8k one"
    assert ordered[-1] == "gpt-4"

    # A measured wide window outranks any claim, and a measured small one sinks
    # to where its real size belongs rather than taking a top slot by luck.
    windows.remember("command-a-03-2025", 65536)
    assert G4fProvider.by_window(["gemini-2.5-pro", "command-a-03-2025"])[0] == "command-a-03-2025"
    windows.remember("gemini-2.5-pro", 4096)
    assert G4fProvider.by_window(["gpt-4", "gemini-2.5-pro"]) == ["gpt-4", "gemini-2.5-pro"]


def test_recommended_models_only_lists_wide_windows():
    from beeagent.core.context import window_for

    picks = G4fProvider.recommended_models(limit=12)
    assert picks, "the catalog has wide-window models"
    assert len(picks) <= 12
    assert all(window_for(m) >= G4fProvider.RECOMMENDED_MIN_TOKENS for m in picks)
    assert picks == G4fProvider.by_window(picks), "already in recommended order"


def test_a_stream_that_dies_mid_answer_is_not_glued_to_the_next_one(monkeypatch):
    """Provider A answers then loses the connection; B answers differently.

    The loop used to swallow A's error and keep going, so the user received
    "ПЕРВАЯ" + "ВТОРАЯ" as one reply and the session stored it as the answer.
    """
    import asyncio
    import sys
    import types

    from beeagent.providers import g4f_provider

    state = {"calls": 0}

    def chunk(text):
        delta = types.SimpleNamespace(content=text)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(delta=delta)])

    class Completions:
        async def create(self, model, messages, stream=True):
            state["calls"] += 1
            first = state["calls"] == 1
            yield chunk("ПЕРВАЯ")
            if first:
                raise RuntimeError("connection reset")
            yield chunk("ВТОРАЯ")

    class Chat:
        completions = Completions()

    class FakeClient:
        def __init__(self, provider=None):
            self.chat = types.SimpleNamespace(completions=Chat.completions)

    monkeypatch.setitem(sys.modules, "g4f.client", types.SimpleNamespace(AsyncClient=FakeClient))
    provider = G4fProvider()

    async def drain():
        collected = []
        async for kind, text in provider.chat_stream([{"role": "user", "content": "x"}],
                                                    model="m"):
            collected.append(text)
        return "".join(collected)

    try:
        answer = asyncio.run(drain())
    except RuntimeError as e:
        assert "connection reset" in str(e), "the real cause must reach the caller"
    else:
        raise AssertionError(f"the broken stream was stitched into: {answer!r}")


def test_g4f_never_hands_the_choice_to_auto_routing(monkeypatch):
    """Every g4f call must name its provider.

    Auto-routing used to be the last entry in the list, and it selects from the
    whole installed catalogue -- which includes endpoints g4f reaches by driving
    a headless browser through a bot check. BeeCode pins providers instead, so
    `AsyncClient` has to be built with a provider every single time.
    """
    import sys
    import types

    pytest.importorskip("g4f", reason="the pinned list is only meaningful with g4f present")
    from beeagent.providers import g4f_provider
    from beeagent.providers.g4f_provider import KEYLESS_PROVIDERS

    seen = []

    class Completions:
        async def create(self, model, messages, stream=False):
            raise RuntimeError("forced failure so the loop is exercised")

    class FakeClient:
        def __init__(self, provider=None):
            seen.append(provider)
            self.chat = types.SimpleNamespace(completions=Completions())

    monkeypatch.setitem(sys.modules, "g4f.client", types.SimpleNamespace(AsyncClient=FakeClient))
    provider = g4f_provider.G4fProvider()

    with pytest.raises(RuntimeError):
        asyncio.run(provider.chat([{"role": "user", "content": "x"}], model="m"))

    assert seen, "the pinned providers should each have been tried"
    assert all(p is not None for p in seen), \
        f"auto-routing was used ({seen.count(None)} bare clients); it may pick a bot-walled endpoint"
    assert len(seen) == len(KEYLESS_PROVIDERS)


def test_no_pinned_keyless_provider_drives_a_browser():
    """The pin list is the whole safety story, so it must stay API-only.

    Introspection cannot express this: g4f builds some providers dynamically
    (LLM7 reports `__module__ == "abc"`), so there is no source to read. The
    names below are the ones whose modules import g4f's browser session, found
    by grepping `Provider/` for CDPSession -- Cloudflare among them, plus a
    captcha solver. Adding one to KEYLESS_PROVIDERS has to fail here.
    """
    from beeagent.providers.g4f_provider import KEYLESS_PROVIDERS

    browser_driven = {
        "Cloudflare", "Copilot", "CopilotSession", "DeepInfra", "Qwen",
        "Gemini", "Grok", "LMArena", "OpenaiChat", "GoogleSearch",
        "GoogleAiMode",
    }
    assert not (set(KEYLESS_PROVIDERS) & browser_driven), \
        "pinned provider needs a headless browser: %s" % sorted(
            set(KEYLESS_PROVIDERS) & browser_driven)


# ===========================================================================
# The client providers — where a free answer becomes the text a user reads.
#
# Every request below is answered by `Wire`, a stand-in for httpx: no test in
# this section has ever opened a socket, so none of them can spend a real key's
# quota or wake the deployed pool. The frames are the shapes those endpoints were
# measured sending: an event split inside a JSON object, `[DONE]` with two
# spaces, an error body after some text, a 200 that is really HTML.
# ===========================================================================

import json                                        # noqa: E402

import httpx                                       # noqa: E402

from beeagent.i18n import get_lang, set_lang       # noqa: E402
from beeagent.providers import crax as crax_mod    # noqa: E402
from beeagent.providers import ollama as ollama_mod            # noqa: E402
from beeagent.providers import openai_compat as compat_mod     # noqa: E402
from beeagent.providers.base import ProviderStreamError        # noqa: E402
from beeagent.providers.crax import CraxProvider              # noqa: E402
from beeagent.providers.ollama import OllamaProvider           # noqa: E402
from beeagent.providers.openai_compat import OpenAICompatProvider  # noqa: E402


@pytest.fixture()
def english():
    """The assertions quote the English wording; the pair is checked separately."""
    previous = get_lang()
    set_lang("en")
    yield
    set_lang(previous)


def delta_frame(text):
    """One OpenAI-shaped delta, framed the way crax and the pool frame it."""
    return "data: " + json.dumps({"choices": [{"index": 0, "delta": {"content": text}}]})


TORN = [delta_frame("Hel"), "", delta_frame("lo the"), "",
        'data: {"choices": [{"delta": {"content": "re"}}', ""]
EMPTY_STREAM = ["data: " + json.dumps({"choices": [{"index": 0, "delta": {}}]}), "",
                "data: [DONE]"]


class Reply:
    """A response carrying exactly the lines the endpoint put on the wire."""

    def __init__(self, lines=None, status=200, body="", headers=None):
        self.status_code = status
        self.headers = headers or {}
        self._lines = list(lines or [])
        self.text = body if body else "".join(line + "\n" for line in self._lines)
        self.is_stream_consumed = False
        self.request = httpx.Request("POST", "http://stub.invalid")

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        self.is_stream_consumed = True
        return self.text.encode()

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self):
        """What httpx does — and what the fixed providers must not need."""
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


class _AsyncClient:
    def __init__(self, wire, *args, **kwargs):
        self.wire, self.client_kw = wire, kwargs
        wire.clients.append(kwargs)

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


class _SyncClient:
    def __init__(self, wire, *args, **kwargs):
        self.wire, self.client_kw = wire, kwargs
        wire.clients.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, url, **kw):
        return self.wire.ask("post", "POST", url, kw, self.client_kw)

    def get(self, url, **kw):
        return self.wire.ask("get", "GET", url, kw, self.client_kw)


class Wire:
    """httpx, replaced: replays one script and remembers what was actually sent."""

    def __init__(self, *modules, respond=None):
        self.respond = respond or (lambda kind, url, kw: Reply(lines=[]))
        self.requests = []
        self.clients = []
        self.modules = modules

    def ask(self, kind, method, url, kw, client_kw=None):
        self.requests.append({"kind": kind, "method": method, "url": url, "kw": kw,
                              "client": client_kw or {}})
        out = self.respond(kind, url, kw)
        if isinstance(out, BaseException):
            raise out
        return out

    def install(self, monkeypatch):
        for module in self.modules:
            monkeypatch.setattr(module.httpx, "AsyncClient",
                                lambda *a, _w=self, **kw: _AsyncClient(_w, *a, **kw))
            monkeypatch.setattr(module.httpx, "Client",
                                lambda *a, _w=self, **kw: _SyncClient(_w, *a, **kw))
        return self

    def urls(self, kind=None):
        return [r["url"] for r in self.requests if kind is None or r["kind"] == kind]

    def keys_used(self):
        """The bearer tokens that went out on the streamed asks, in order."""
        return [(r["kw"].get("headers") or {}).get("Authorization")
                for r in self.requests if r["kind"] == "stream"]

    def effective_timeout(self, needle=""):
        """The read timeout a request went out under, per-call or per-client."""
        for entry in reversed(self.requests):
            if needle and needle not in entry["url"]:
                continue
            timeout = entry["kw"].get("timeout") or entry["client"].get("timeout")
            if timeout is None:
                continue
            return timeout.read if hasattr(timeout, "read") else float(timeout)
        return None


def collect(agen):
    async def drain():
        return [pair async for pair in agen]

    return asyncio.run(drain())


def collect_with_error(agen):
    """The pieces that reached the screen, plus the error that followed them."""
    async def drain():
        pieces, error = [], None
        try:
            async for pair in agen:
                pieces.append(pair)
        except Exception as exc:          # noqa: BLE001 — the test names the type
            error = exc
        return pieces, error

    return asyncio.run(drain())


def seen(pieces):
    return "".join(text for _, text in pieces)


def run(coro):
    return asyncio.run(coro)


# --- crax: the answer is whole, or it is an error --------------------------

def test_crax_does_not_hand_a_torn_stream_back_as_an_answer(monkeypatch, english):
    """Measured: "I will now write the file and the third", no error, one POST.

    The provider was reading `aiter_lines()` line by line and dropping the frame
    it could not parse, which is the worst bug this project counts: a half answer
    sold as a complete one.
    """
    wire = Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=TORN)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "qwen3-coder-480b"))

    assert seen(pieces) == "Hello the", "the text that did arrive is what got shown"
    assert isinstance(error, ProviderStreamError), f"the half answer ended quietly: {error!r}"
    assert "cannot be read" in str(error) and "not the whole answer" in str(error)
    assert len(wire.urls("stream")) == 1, "a torn answer is not silently asked again"


@pytest.mark.parametrize("marker", ["data:  [DONE]", "[DONE]", "data: [DONE]", "data:[DONE]",
                                    'data: "[DONE]"'])
def test_crax_stops_at_every_spelling_of_done(monkeypatch, english, marker):
    """`data:  [DONE]` was not a match, so post-DONE junk joined the answer."""
    lines = [delta_frame("real answer"), "", marker, "", delta_frame(" <<GARBAGE>>"), ""]
    Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "qwen3-coder-480b"))

    assert seen(pieces) == "real answer"
    assert error is None, "a clean [DONE] is not a failure"


def test_crax_keeps_one_event_spread_over_two_data_lines(monkeypatch, english):
    frames = ['data: {"choices": [{"delta": {"content":',
              'data:  "the whole answer"}}]}', "", "data: [DONE]"]
    Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=frames)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    assert collect(provider.chat_stream([{"role": "user", "content": "х"}],
                                        "qwen3-coder-480b")) == [("content", "the whole answer")]


def test_crax_tells_the_reason_from_an_error_frame_after_text(monkeypatch, english):
    """A refusal mid-stream used to leave the truncated text as the final answer."""
    lines = [delta_frame("half an "), "", "data: " + json.dumps(
        {"error": {"message": "model 'grok-4-6' is overloaded, retry later"}}), ""]
    wire = Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    pieces, error = collect_with_error(
        provider.chat_stream([{"role": "user", "content": "х"}], "grok-4-6"))

    assert "grok-4-6' is overloaded" in str(error), f"the reason was dropped: {error!r}"
    assert error is not None and seen(pieces) == "half an "
    assert len(wire.urls("stream")) == 1


def test_crax_tells_the_reason_from_an_error_frame_alone(monkeypatch, english):
    """Before: "crax-gpt streamed nothing", with the endpoint's sentence thrown away."""
    lines = ["data: " + json.dumps({"error": {"message": "model 'grok-4-6' is overloaded"}}), ""]
    Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    with pytest.raises(ProviderStreamError) as raised:
        collect(provider.chat_stream([{"role": "user", "content": "х"}], "grok-4-6"))
    assert "model 'grok-4-6' is overloaded" in str(raised.value)
    assert "streamed nothing" not in str(raised.value)


def test_crax_asks_an_empty_stream_once_more_and_of_another_key(monkeypatch, english):
    """Measured: 9 upstream POSTs, 3 keys, one user message — all charged to key 1.

    A 200 that streamed nothing is worth one extra ask, and the key that gave it
    is not the one to ask: the same key answers the same way, and the seat pays.
    """
    wire = Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=EMPTY_STREAM)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one,key-two,key-three")

    with pytest.raises(ProviderStreamError) as raised:
        collect(provider.chat_stream([{"role": "user", "content": "hi"}], "qwen3-coder-480b"))

    asks = wire.urls("stream")
    assert len(asks) == 2, f"the retry is not bounded: {len(asks)} POSTs"
    keys = [k.split()[-1] for k in wire.keys_used()]
    assert keys == ["key-one", "key-two"], f"the same key was billed twice: {keys}"
    assert "without an answer" in str(raised.value)
    assert provider.serves("qwen3-coder-480b")


def test_crax_does_not_ask_a_key_it_already_knows_is_busy(monkeypatch, english):
    """The spare key carries the retry; a cooling account is left alone."""
    from time import time

    provider = CraxProvider(api_key="key-one,key-two")
    provider.spent[0] = time() + 60          # key one is cooling down for another minute
    wire = Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=EMPTY_STREAM)).install(monkeypatch)

    with pytest.raises(ProviderStreamError):
        collect(provider.chat_stream([{"role": "user", "content": "hi"}], "qwen3-coder-480b"))

    assert [k.split()[-1] for k in wire.keys_used()] == ["key-two"], \
        f"a cooling key was billed anyway: {wire.keys_used()}"


def test_crax_retries_a_per_ip_limit_on_the_same_key(monkeypatch, english):
    """Another account is not a cure for an IP limit — the wait is.

    The rotation must not spend key two on a limit key one can be waited out of.
    """
    asks = []

    def respond(kind, url, kw):
        asks.append((url, (kw.get("headers") or {}).get("Authorization")))
        if len(asks) == 1:
            return Reply(status=429,
                         body=json.dumps({"error": {"type": "rate_limit_exceeded",
                                                    "message": "Too many requests"}}),
                         headers={"Retry-After": "1"})
        return Reply(lines=[delta_frame("раз"), "", "data: [DONE]"])

    async def ask(error):
        return "wait"

    async def no_sleep(seconds):
        return None

    Wire(crax_mod, respond=respond).install(monkeypatch)
    monkeypatch.setattr(crax_mod, "_sleep", no_sleep)
    provider = CraxProvider(api_key="key-one,key-two", ask=ask)

    pieces = collect(provider.chat_stream([{"role": "user", "content": "х"}], "qwen3-coder-480b"))

    assert seen(pieces) == "раз"
    assert [auth.split()[-1] for _, auth in asks] == ["key-one", "key-one"], \
        "a per-IP limit moved the request to another account"


# --- crax / pool: the catalogue the provider reports is the live one -------

def test_crax_reports_the_live_catalogue_once_the_endpoint_answered(monkeypatch, english):
    """Routing checked a tuple compiled into the package, never the live list."""
    body = json.dumps({"data": [{"id": "llama-4-scout"}, {"id": "seedream-5"},
                                {"id": "qwen3-coder-480b"}]})
    Wire(crax_mod, respond=lambda k, u, kw: Reply(status=200, body=body)).install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    assert provider.discover_models() == ["llama-4-scout", "qwen3-coder-480b"]
    assert provider.models == ["llama-4-scout", "qwen3-coder-480b"]
    assert provider.model_list_state == "live"
    assert provider.serves("llama-4-scout") is True
    assert provider.serves("glm-5.2") is False, "the endpoint stopped serving it, the tuple did not"


def test_crax_never_confuses_a_blocked_catalogue_with_a_live_one(monkeypatch, english):
    """crax's Cloudflare answers 1010 to some networks; the offline list is honest
    only when the user is told it is the offline list."""
    Wire(crax_mod, respond=lambda k, u, kw: Reply(status=403,
                                                  body="<html>error code 1010</html>"))\
        .install(monkeypatch)
    provider = CraxProvider(api_key="key-one")

    assert provider.discover_models() == list(CraxProvider.models)
    assert provider.model_list_state != "live"
    assert "ships" in provider.model_list_note() or "BeeCode" in provider.model_list_note()


# --- openai_compat: an error is not a model list --------------------------

def test_a_401_is_not_turned_into_a_model_list(monkeypatch, english):
    """Measured: `list_models()` answered the declared names for a key that was
    refused, and the cache layer wrote them down as the endpoint's own list for
    an hour. A refusal is not an answer, and it must not be read as one."""
    body = json.dumps({"error": {"message": "API key is invalid"}})
    Wire(compat_mod, respond=lambda k, u, kw: Reply(status=401, body=body)).install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="bad",
                                    model="llama-3.3-70b-versatile", name="groq",
                                    models=("llama-3.3-70b-versatile",))

    with pytest.raises(RuntimeError) as raised:
        run(provider.list_models())

    assert "API key is invalid" in str(raised.value)
    assert provider.model_list_state == "refused", \
        f"the refusal was filed as a live list: {provider.model_list_state}"
    assert provider.models == ["llama-3.3-70b-versatile"], "the picker still shows something"
    assert "API key is invalid" in provider.model_list_note()


def test_a_model_the_endpoint_lists_is_one_it_will_serve(monkeypatch, english):
    body = json.dumps({"data": [{"id": "llama-4-scout"}, {"id": "qwen3-32b"}]})
    Wire(compat_mod, respond=lambda k, u, kw: Reply(status=200, body=body)).install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k",
                                    model="llama-3.1-8b-instant", name="groq",
                                    models=("llama-3.1-8b-instant",))

    listed = run(provider.list_models())

    assert listed == ["llama-4-scout", "qwen3-32b"], "the endpoint's list, in its order"
    assert provider.model_list_state == "live"
    assert provider.serves("llama-4-scout") is True, \
        "the picker offered a model routing then rewrote to another"
    assert provider.models == ["llama-4-scout", "qwen3-32b"], "`models` is still the shipped tuple"


def test_an_endpoints_own_words_survive_a_429(monkeypatch, english):
    """`raise_for_status()` threw the body away, and the body had the diagnosis."""
    body = json.dumps({"error": {"type": "rate_limit_exceeded",
                                 "message": "Daily token allowance exceeded for key"}})
    Wire(compat_mod, respond=lambda k, u, kw: Reply(status=429, body=body,
                                                    headers={"Retry-After": "30"}))\
        .install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq")

    with pytest.raises(Exception) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))

    text = str(raised.value)
    assert "Daily token allowance exceeded" in text, f"the reason was dropped: {text!r}"
    assert "Client error" not in text


def test_a_200_that_is_html_says_it_was_html(monkeypatch, english):
    """`Expecting value: line 1 column 1` is not a sentence a user can act on."""
    html = "<html><head><title>Attention Required! | Cloudflare</title></head><body>x</body></html>"
    Wire(compat_mod, respond=lambda k, u, kw: Reply(status=200, body=html)).install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq")

    with pytest.raises(Exception) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))

    text = str(raised.value)
    assert "Expecting value" not in text, text
    assert "json" in text.lower() or "html" in text.lower(), text
    assert "Attention Required" in text, f"the page's own words are missing: {text!r}"


def test_a_stream_refused_mid_handshake_is_not_read_as_an_empty_answer(monkeypatch, english):
    lines = ["data: " + json.dumps({"error": {"message": "model 'x' is overloaded"}}), ""]
    Wire(compat_mod, respond=lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq")

    with pytest.raises(ProviderStreamError) as raised:
        collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))
    assert "model 'x' is overloaded" in str(raised.value)


# --- timeouts: the agent's budget is the client's budget ------------------

def test_the_preset_endpoint_waits_as_long_as_the_agent_says(monkeypatch, english):
    """60 s of read timeout against a 90 s idle budget: a slow model died first,
    and it died as a bare ReadTimeout rather than as the stall the agent names."""
    wire = Wire(compat_mod, respond=lambda k, u, kw: Reply(
        lines=[delta_frame("a"), "", "data: [DONE]"])).install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq",
                                    idle_timeout=123.0)

    collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert provider._timeout().read == 123.0
    assert wire.effective_timeout("chat/completions") == 123.0, \
        "the request went out under a shorter budget than the agent's own"


def test_the_preset_endpoint_defaults_to_the_configs_idle_budget(monkeypatch, english):
    """A provider built by hand (a custom_providers entry) still gets the default."""
    from beeagent.config.schema import BeeConfig

    Wire(compat_mod, respond=lambda k, u, kw: Reply(lines=[delta_frame("a"), "", "data: [DONE]"]))\
        .install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq")

    collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))

    assert provider.idle_timeout == float(BeeConfig().stream_idle_timeout), \
        "the read timeout is shorter than the budget the agent narrates a stall with"


def test_ollama_waits_for_a_local_model_that_thinks(monkeypatch, english):
    from beeagent.config.schema import BeeConfig

    Wire(ollama_mod, respond=lambda k, u, kw: Reply(
        lines=[json.dumps({"message": {"content": "раз"}}), ""]))\
        .install(monkeypatch)
    provider = OllamaProvider(base_url="http://localhost:11434")

    assert seen(collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))) == "раз"
    assert provider.idle_timeout == float(BeeConfig().stream_idle_timeout)


def test_a_timeout_is_named_in_words_and_not_as_a_class(monkeypatch, english):
    """An httpx timeout carries no message: the retry report ended "(last error: )"."""
    Wire(compat_mod, respond=lambda k, u, kw: httpx.ReadTimeout(""))\
        .install(monkeypatch)
    provider = OpenAICompatProvider(base_url="http://stub.invalid", api_key="k", name="groq",
                                    idle_timeout=42.0)

    with pytest.raises(RuntimeError) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))
    text = str(raised.value).strip()
    assert text and "ReadTimeout" not in text and "42" in text, repr(text)


# --- ollama: the reason it gave is the reason we show ---------------------

def test_ollamas_model_not_found_reaches_the_user(monkeypatch, english):
    """Before: `Client error '404 Not Found' for url 'http://localhost:11434/...'`."""
    body = json.dumps({"error": "model 'llama3:latest' not found"})
    Wire(ollama_mod, respond=lambda k, u, kw: Reply(status=404, body=body)).install(monkeypatch)
    provider = OllamaProvider(base_url="http://localhost:11434")

    with pytest.raises(Exception) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))

    text = str(raised.value)
    assert "model 'llama3:latest' not found" in text, f"the reason was thrown away: {text!r}"
    assert "Client error" not in text


def test_ollama_still_reads_the_undecorated_ndjson_it_actually_sends(monkeypatch, english):
    """The frame shape is Ollama's own; only the reader is shared."""
    lines = [json.dumps({"message": {"role": "assistant", "content": "раз"}}),
             json.dumps({"message": {"role": "assistant", "content": ""}, "done": False}),
             json.dumps({"message": {"role": "assistant", "content": "два"}}),
             json.dumps({"message": {"role": "assistant"}, "done": True})]
    Wire(ollama_mod, respond=lambda k, u, kw: Reply(lines=lines)).install(monkeypatch)
    provider = OllamaProvider(base_url="http://localhost:11434")

    assert seen(collect(provider.chat_stream([{"role": "user", "content": "х"}], "m"))) == "раздва"


def test_ollama_says_what_arrived_when_the_answer_is_not_json(monkeypatch, english):
    Wire(ollama_mod, respond=lambda k, u, kw: Reply(
        status=200, body="<html><title>502 Bad Gateway</title></html>")).install(monkeypatch)
    provider = OllamaProvider(base_url="http://localhost:11434")

    with pytest.raises(Exception) as raised:
        run(provider.chat([{"role": "user", "content": "х"}], "m"))
    assert "Expecting value" not in str(raised.value)
    assert "502 Bad Gateway" in str(raised.value)


# --- one reader, four providers -------------------------------------------

def test_all_four_providers_read_their_streams_through_the_same_reader(
        monkeypatch, english, tmp_path):
    """The same accident must fail the same way wherever it arrives.

    Each provider keeps its own frame shape — crax and the pool answer OpenAI
    deltas, Ollama answers in newline-delimited JSON — and all four hand those
    lines to the one reader in `base.py`, so a stream cut inside an object can
    never be mistaken for a finished answer by any of them.
    """
    from beeagent.providers import pool as pool_mod

    def ndjson(text):
        return json.dumps({"message": {"role": "assistant", "content": text}})

    ollama_torn = [ndjson("Hel"), ndjson("lo the"), '{"message": {"content": "re"']

    monkeypatch.setenv("BEECODE_POOL_KEY_FILE", str(tmp_path / "pool-key.json"))
    providers = {
        "pool": (pool_mod.PoolProvider(url="http://stub.invalid", token="seat"), TORN),
        "crax": (CraxProvider(api_key="key-one"), TORN),
        "openai_compat": (OpenAICompatProvider(base_url="http://stub.invalid", name="groq"), TORN),
        "ollama": (OllamaProvider(base_url="http://localhost:11434"), ollama_torn),
    }

    for name, (provider, lines) in providers.items():
        Wire(crax_mod, pool_mod, compat_mod, ollama_mod,
             respond=lambda k, u, kw, _l=lines: Reply(lines=_l)).install(monkeypatch)
        pieces, error = collect_with_error(
            provider.chat_stream([{"role": "user", "content": "х"}], "m"))
        assert seen(pieces) == "Hello the", f"{name} lost the text that did arrive"
        assert isinstance(error, ProviderStreamError), f"{name} ended a torn stream quietly"
        assert "cannot be read" in str(error), f"{name} invented its own wording: {error}"


def test_the_new_honesties_are_written_in_two_languages(monkeypatch):
    """Every user-facing string here is a pair; an untranslated one is a bug."""
    previous = get_lang()
    set_lang("ru")
    try:
        Wire(crax_mod, respond=lambda k, u, kw: Reply(lines=TORN)).install(monkeypatch)
        _, crax_error = collect_with_error(
            CraxProvider(api_key="key-one").chat_stream([{"role": "user", "content": "х"}], "m"))
        Wire(ollama_mod, respond=lambda k, u, kw: Reply(
            status=404, body=json.dumps({"error": "model not found"}))).install(monkeypatch)
        with pytest.raises(Exception) as ollama_error:
            run(OllamaProvider(base_url="http://localhost:11434")
                .chat([{"role": "user", "content": "х"}], "m"))
    finally:
        set_lang(previous)

    assert "не читается" in str(crax_error) and "не весь ответ" in str(crax_error)
    assert "ответил" in str(ollama_error.value), str(ollama_error.value)

