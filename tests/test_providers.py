import asyncio
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
    # curated quick picks come first
    assert p.models[0] == "glm-4.7-flash"


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
