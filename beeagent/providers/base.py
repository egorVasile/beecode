from abc import ABC, abstractmethod
from typing import AsyncIterator

class BaseProvider(ABC):
    name: str = ""
    models: list[str] = []
    
    @abstractmethod
    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        ...
    
    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[str]:
        raise NotImplementedError
