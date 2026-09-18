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

def test_provider_fallback():
    registry = ProviderRegistry()
    registry.register(DummyProvider())
    p = registry.fallback("nonexistent")
    assert p.name == "dummy"

def test_provider_fallback_preferred():
    registry = ProviderRegistry()
    
    class OtherProvider(BaseProvider):
        name = "other"
        models = ["other-model"]
        async def chat(self, messages, model="", stream=False):
            return "other"
    
    registry.register(DummyProvider())
    registry.register(OtherProvider())
    p = registry.fallback("other")
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
