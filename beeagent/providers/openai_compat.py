import json
from typing import AsyncIterator

import httpx

from .base import BaseProvider


class OpenAICompatProvider(BaseProvider):
    """Any endpoint speaking the OpenAI chat-completions protocol.

    Used both by `custom_providers` in beeagent.json and by the free-tier
    presets in `providers/presets.py`, where `name` becomes the provider name
    the user picks with `/provider groq` and friends.
    """

    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4",
                 name: str = "openai_compat", models: list | tuple = ()):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = model
        self.name = name
        # Known models of this endpoint; /models asks the endpoint for the real list.
        self.models = list(models) if models else [model]

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    async def list_models(self) -> list[str]:
        """Ask the endpoint itself which models this key may use."""
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
                resp.raise_for_status()
                data = resp.json()
        except Exception:
            return list(self.models)
        ids = [m.get("id") or m.get("name") for m in data.get("data", []) if isinstance(m, dict)]
        return sorted({i for i in ids if i}) or list(self.models)

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        model = model or self.default_model
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                json={"model": model, "messages": messages, "stream": stream},
                headers=self._headers(),
                timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[str]:
        model = model or self.default_model
        headers = self._headers()
        headers["Accept"] = "text/event-stream"
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                json={"model": model, "messages": messages, "stream": True},
                headers=headers,
                timeout=60,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    payload = line[6:]
                    if payload.strip() == "[DONE]":
                        return
                    try:
                        chunk = json.loads(payload)
                    except json.JSONDecodeError:
                        continue
                    choices = chunk.get("choices") or []
                    if not choices:
                        continue
                    piece = (choices[0].get("delta") or {}).get("content")
                    if piece:
                        yield piece
