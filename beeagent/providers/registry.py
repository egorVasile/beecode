from typing import Optional
from .base import BaseProvider

class ProviderRegistry:
    def __init__(self):
        self._providers: dict[str, BaseProvider] = {}
    
    def register(self, provider: BaseProvider):
        self._providers[provider.name] = provider
    
    def get(self, name: str) -> Optional[BaseProvider]:
        return self._providers.get(name)
    
    def list_names(self) -> list[str]:
        return list(self._providers.keys())
    
    def fallback(self, preferred: str) -> BaseProvider:
        if preferred in self._providers:
            return self._providers[preferred]
        for p in self._providers.values():
            return p
        raise RuntimeError("No providers available")
