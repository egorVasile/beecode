"""A local Ollama daemon — the provider that never needs a key.

`/api/chat` answers in newline-delimited JSON rather than SSE, which the shared
reader handles as one of the shapes it knows; what this file adds is Ollama's own
frame, and the refusal it puts in a 200 body.
"""
from typing import AsyncIterator

import httpx

from .base import (BaseProvider, ProviderAPIError, check_error_frame,
                   default_idle_timeout, parse_json_body, raise_for_api_status,
                   read_answer_stream, transport_error)

CONNECT_TIMEOUT = 10.0
NAME = "ollama"


class OllamaProvider(BaseProvider):
    name = NAME
    list_source = (NAME, NAME)

    def __init__(self, base_url: str = "http://localhost:11434", model: str = "llama3",
                 idle_timeout: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.default_model = model
        self.models = [model]
        # A local model on a phone answers slowly; the budget the agent uses to
        # explain a silence is the budget this client waits on, not a number
        # invented beside the request.
        self.idle_timeout = float(idle_timeout) if idle_timeout else default_idle_timeout()

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.idle_timeout, connect=CONNECT_TIMEOUT)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout())

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        model = model or self.default_model
        try:
            async with self._client() as client:
                resp = await client.post(
                    f"{self.base_url}/api/chat",
                    json={"model": model, "messages": messages, "stream": False},
                )
        except httpx.HTTPError as e:
            raise transport_error(e, NAME, self.idle_timeout) from e
        # Not `raise_for_status()`: Ollama's "model 'llama3:latest' not found" is
        # in the body of that 404, and dropping it left the user with a status
        # line and a url — nothing to fix.
        await raise_for_api_status(resp, NAME)
        data = parse_json_body(resp.text or "", NAME)
        check_error_frame(data, NAME, exc=ProviderAPIError)
        message = data.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return content if isinstance(content, str) else ""

    @staticmethod
    def _pieces(event: dict) -> list[tuple[str, str]]:
        """The text one Ollama record carries.

        `done` and the empty final `{"message":{"role":"assistant"}}` are not
        answers, and neither is an `{"error": ...}` body — that one says why.
        """
        check_error_frame(event, NAME)
        message = event.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        return [("content", content)] if isinstance(content, str) and content else []

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[tuple[str, str]]:
        model = model or self.default_model
        try:
            async with self._client() as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/api/chat",
                    json={"model": model, "messages": messages, "stream": True},
                    headers={"Accept": "text/event-stream"},
                ) as resp:
                    await raise_for_api_status(resp, NAME)
                    # The body of /api/chat is newline-delimited JSON, and a proxy
                    # in front of it may wrap those records in `data:` frames or
                    # answer with `[DONE]`; `read_answer_stream` is the one reader
                    # that copes with both spellings, joins a record split over
                    # several lines, ignores keep-alives, and refuses to pass off a
                    # stream with no text in it as an empty answer — which `chat()`
                    # would report as a provider failure anyway.
                    async for piece in read_answer_stream(resp.aiter_lines(), self._pieces,
                                                          source=NAME):
                        yield piece
        except httpx.HTTPError as e:
            raise transport_error(e, NAME, self.idle_timeout) from e
