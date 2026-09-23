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
    assert "gpt-4" in p.models or "gpt-3.5-turbo" in p.models

def test_g4f_provider_is_base():
    from beeagent.providers.base import BaseProvider
    p = G4fProvider()
    assert isinstance(p, BaseProvider)


def test_g4f_curated_models_include_glm():
    p = G4fProvider()
    assert "glm-4.7-flash" in p.models
    assert "glm-5.2" in p.models
    # the route measured to carry a 64k prompt keyless leads the list
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


def test_discover_models_is_full_catalog():
    catalog = G4fProvider.discover_models()
    # curated list first, then every model advertised by working providers
    assert catalog[:len(G4fProvider.models)] == G4fProvider.models
    assert "glm-4.7-flash" in catalog
    assert len(catalog) > 100   # installed g4f advertises hundreds
    assert len(catalog) == len(set(catalog))  # no duplicates


def test_available_models_uses_full_catalog():
    from beeagent.ui.commands import ReplContext, available_models
    from beeagent.config.schema import BeeConfig
    from beeagent.core.session import Session

    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    models = available_models(ctx)
    assert "glm-4.7-flash" in models
    assert "gpt-4" in models


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

