from typing import AsyncIterator

import httpx

from .base import BaseProvider, check_error_frame, read_answer_stream


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

    @staticmethod
    def _pieces(event: dict) -> list[tuple[str, str]]:
        """The text one Ollama record carries.

        `done` and the empty final `{"message":{"role":"assistant"}}` are not
        answers, and neither is an `{"error": ...}` body — that one says why.
        """
        check_error_frame(event, "ollama")
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return [("content", content)] if isinstance(content, str) and content else []

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[tuple[str, str]]:
        model = model or self.default_model
        async with httpx.AsyncClient() as client:
            async with client.stream(
                "POST",
                f"{self.base_url}/api/chat",
                json={"model": model, "messages": messages, "stream": True},
                headers={"Accept": "text/event-stream"},
                timeout=120,
            ) as resp:
                resp.raise_for_status()
                # The body of /api/chat is newline-delimited JSON, and a proxy in
                # front of it may wrap those records in `data:` frames or answer with
                # `[DONE]`; `read_answer_stream` is the one reader that copes with
                # both spellings, joins a record split over several lines, ignores
                # keep-alives, and refuses to pass off a stream with no text in it
                # as an empty answer — which `chat()` would report as a provider
                # failure anyway.
                async for piece in read_answer_stream(resp.aiter_lines(), self._pieces,
                                                      source="ollama"):
                    yield piece
