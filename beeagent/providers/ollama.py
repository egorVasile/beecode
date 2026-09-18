import httpx
from typing import AsyncIterator
from .base import BaseProvider

class OllamaProvider(BaseProvider):
    name = "ollama"
    
    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3"):
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.models = [model]
    
    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        model = model or self.default_model
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["message"]["content"]
    
    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[str]:
        model = model or self.default_model
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
                timeout=120,
            ) as resp:
                import json
                async for line in resp.aiter_lines():
                    if line:
                        data = json.loads(line)
                        if data.get("message", {}).get("content"):
                            yield data["message"]["content"]
