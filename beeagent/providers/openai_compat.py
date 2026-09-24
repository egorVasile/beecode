from typing import AsyncIterator

import httpx

from .base import BaseProvider, check_error_frame, read_answer_stream


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

    @staticmethod
    def _pieces(event: dict) -> list[tuple[str, str]]:
        """The text one chat-completion frame carries.

        An `error` frame is the endpoint explaining itself; swallowing it is how
        "no such model" became "the provider said nothing".
        """
        check_error_frame(event)
        choices = event.get("choices")
        first = choices[0] if isinstance(choices, list) and choices else None
        if not isinstance(first, dict):
            return []
        delta = first.get("delta")
        content = delta.get("content") if isinstance(delta, dict) else None
        return [("content", content)] if isinstance(content, str) and content else []

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[tuple[str, str]]:
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
                # One line is not one event. `read_answer_stream` is the SSE reader
                # for both providers: it joins the several `data:` lines of a single
                # event, treats CRLF and a bare `data:{...}` alike, skips
                # `: keep-alive` comments, stops at `[DONE]` without ever letting it
                # become answer text, and raises rather than ending quietly when the
                # stream carried nothing — the same promise Ollama makes.
                async for piece in read_answer_stream(resp.aiter_lines(), self._pieces,
                                                      source=self.name):
                    yield piece
