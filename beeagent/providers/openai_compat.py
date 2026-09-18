import httpx
from typing import AsyncIterator
from .base import BaseProvider

class OpenAICompatProvider(BaseProvider):
    name = "openai_compat"
    
    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4"):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = model
        self.models = [model]
    
    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        model = model or self.default_model
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json={"model": model, "messages": messages, "stream": stream},
                headers=headers,
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
    
    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[str]:
        model = model or self.default_model
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/v1/chat/completions",
                json={"model": model, "messages": messages, "stream": True},
                headers=headers,
                timeout=60,
            ) as resp:
                import json
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data = json.loads(line[6:])
                        if data["choices"][0]["delta"].get("content"):
                            yield data["choices"][0]["delta"]["content"]
