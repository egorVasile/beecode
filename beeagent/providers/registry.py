from typing import Optional

from beeagent.i18n import L

from .base import BaseProvider

class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}
    
    def register(self, provider: BaseProvider):
        self._providers[provider.name] = provider

    def unregister(self, name: str) -> bool:
        """Drop a provider; True when one was actually removed."""
        return self._providers.pop(name, None) is not None
    
    def get(self, name: str) -> Optional[BaseProvider]:
        return self._providers.get(name)
    
    def list_names(self) -> list[str]:
        return list(self._providers.keys())
    
    def select(self, preferred: str) -> BaseProvider:
        """The provider the user asked for, or a clear error.

        This used to hand back the first registered provider when the name was
        unknown, so a typo in `provider` — or a custom provider nobody
        configured — silently kept sending everything to g4f.
        """
        provider = self._providers.get(preferred)
        if provider is None:
            available = ", ".join(self._providers) or L("none", "ни одного")
            raise RuntimeError(L(
                f"provider '{preferred}' is not configured — available: {available} "
                f"(run /providers)",
                f"провайдер '{preferred}' не настроен — доступны: {available} "
                f"(выполни /providers)",
            ))
        return provider
