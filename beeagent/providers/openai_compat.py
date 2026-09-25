"""An endpoint that speaks the OpenAI chat-completions protocol.

Used both by `custom_providers` in beeagent.json and by the free-tier presets in
`providers/presets.py`, where `name` becomes the provider name the user picks
with `/provider groq` and friends.

Two promises run through this file. A refusal is never presented as an answer —
not a 401 turned into a model list, not a gateway's HTML page turned into a
JSON parse error, not an error frame turned into silence. And the whole answer
arrives or the attempt fails: the reading of the stream is the shared reader in
`base.py`, the same one the pool, crax and Ollama answer through.
"""
from typing import AsyncIterator

import httpx

from .base import (LIST_REFUSED, LIST_UNREACHABLE, BaseProvider, ProviderAPIError,
                   api_error, body_reason, check_error_frame, default_idle_timeout,
                   parse_json_body, raise_for_api_status, read_answer_stream,
                   transport_error)

CONNECT_TIMEOUT = 10.0


class OpenAICompatProvider(BaseProvider):
    def __init__(self, base_url: str, api_key: str = "", model: str = "gpt-4",
                 name: str = "openai_compat", models: list | tuple = (),
                 idle_timeout: float | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.default_model = model
        self.name = name
        # Known models of this endpoint; /models asks the endpoint for the real
        # list, and once it has answered `self.models` is that answer.
        self.models = list(models) if models else [model]
        self.list_source = (name or "the endpoint", name or "эндпоинт")
        # The agent's own silence budget, not a number picked beside the request:
        # a read timeout shorter than `stream_idle_timeout` kills a slow model
        # before the agent ever gets to say the endpoint has gone quiet, and the
        # user reads a bare timeout as a broken provider.
        self.idle_timeout = float(idle_timeout) if idle_timeout else default_idle_timeout()

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.idle_timeout, connect=CONNECT_TIMEOUT)

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=self._timeout())

    async def list_models(self) -> list[str]:
        """Ask the endpoint itself which models this key may use.

        A refusal is not a catalogue. This used to answer a 401 with the names
        compiled into `providers/presets.py`, and the `/models` cache wrote them
        down as the endpoint's own answer for an hour — so an invalid key looked
        like a healthy list of models, and the picker offered them.

        Now a refusal raises, the state says which of the two happened, and the
        shipped list stays labelled as the fallback it is.
        """
        try:
            async with self._client() as client:
                response = await client.get(f"{self.base_url}/models", headers=self._headers())
        except httpx.HTTPError as e:
            failure = transport_error(e, self.name, self.idle_timeout)
            self.report_model_list_failure(LIST_UNREACHABLE, str(failure))
            raise failure from e
        if response.status_code >= 400:
            text = response.text or ""
            self.report_model_list_failure(LIST_REFUSED, body_reason(text))
            raise api_error(response.status_code, text, self.name)
        data = parse_json_body(response.text or "", self.name)
        found = self.remember_live_models(
            [item.get("id") or item.get("name") for item in data.get("data") or []
             if isinstance(item, dict)])
        if not found:
            from beeagent.i18n import L
            self.report_model_list_failure(
                LIST_REFUSED, L("the endpoint listed no models for this key",
                                "эндпоинт не назвал ни одной модели для этого ключа"))
        return found

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        model = model or self.default_model
        try:
            async with self._client() as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={"model": model, "messages": messages, "stream": stream},
                    headers=self._headers(),
                )
        except httpx.HTTPError as e:
            raise transport_error(e, self.name, self.idle_timeout) from e
        # Not `raise_for_status()`: that keeps the status and throws away the
        # sentence beside it, and the sentence is the part the user can act on.
        await raise_for_api_status(response, self.name)
        data = parse_json_body(response.text or "", self.name)
        # Some gateways answer 200 and put the refusal in the body. It is a
        # refusal, not an answer of no characters.
        check_error_frame(data, self.name, exc=ProviderAPIError)
        choices = data.get("choices") or []
        first = choices[0] if isinstance(choices, list) and choices else {}
        message = first.get("message") if isinstance(first, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        return content if isinstance(content, str) else ""

    @staticmethod
    def _pieces(event: dict, source: str = "") -> list[tuple[str, str]]:
        """The text one chat-completion frame carries.

        An `error` frame is the endpoint explaining itself; swallowing it is how
        "no such model" became "the provider said nothing".
        """
        check_error_frame(event, source)
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

        def pieces(event: dict) -> list[tuple[str, str]]:
            return self._pieces(event, self.name)

        try:
            async with self._client() as client:
                async with client.stream(
                    "POST",
                    f"{self.base_url}/chat/completions",
                    json={"model": model, "messages": messages, "stream": True},
                    headers=headers,
                ) as resp:
                    await raise_for_api_status(resp, self.name)
                    # One line is not one event. `read_answer_stream` is the SSE
                    # reader for all four providers: it joins the several `data:`
                    # lines of a single event, treats CRLF and a bare `data:{...}`
                    # alike, skips `: keep-alive` comments, stops at `[DONE]` in
                    # every spelling without ever letting it become answer text,
                    # and raises rather than ending quietly when the stream
                    # carried nothing.
                    async for piece in read_answer_stream(resp.aiter_lines(), pieces,
                                                          source=self.name):
                        yield piece
        except httpx.HTTPError as e:
            raise transport_error(e, self.name, self.idle_timeout) from e
