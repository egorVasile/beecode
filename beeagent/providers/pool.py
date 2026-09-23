"""The `pool` provider — BeeCode talking to your own key pool instead of the keys.

Nothing here holds a credential. The pool does, and this client proves who it is
with the seat token the pool gave it once (`/pool enroll`). That ordering is the
whole design: a key that is never sent to a client cannot be read off one.

Errors are written to be actionable, because the failure modes are the user's
problem and not ours to hide: no seat yet, seat awaiting approval, today's
budget spent, every key rate-limited.
"""
from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

import httpx

from .base import BaseProvider

TIMEOUT_CONNECT = 10.0
# A free Render instance sleeps after fifteen idle minutes, and the platform's own
# dashboard says a request that wakes it can wait "50 seconds or more" — measured
# against this pool on 2026-09-23, so the wait is set to cover what they promise
# rather than what sounds reasonable. Without it the first ask of the day reports
# a working pool as broken.
COLD_START_WAIT = 55.0


class PoolError(RuntimeError):
    pass


_MESSAGE_FOR = {
    401: ("this BeeCode has no seat in the pool — run /pool enroll",
          "у этого BeeCode нет места в пуле — выполни /pool enroll"),
    403: ("the pool owner has not approved this seat yet",
          "владелец пула ещё не подтвердил это место"),
    413: ("the prompt is bigger than the pool accepts",
          "запрос больше, чем пул принимает"),
    429: ("the pool is rate-limited right now, or today's budget is spent",
          "пул сейчас на лимите или дневная норма выбрана"),
    502: ("the pool could not reach the provider",
          "пул не смог дойти до провайдера"),
    503: ("the pool has no keys for that provider",
          "в пуле нет ключей для этого провайдера"),
}


def _reason(status: int, body: object) -> str:
    detail = ""
    if isinstance(body, dict):
        detail = str(body.get("error") or "")
    known = _MESSAGE_FOR.get(status)
    if known:
        from beeagent.i18n import L
        text = L(known[0], known[1])
        return f"{text}{f' — {detail}' if detail and detail not in text else ''}"
    return f"the pool answered {status}{f' — {detail}' if detail else ''}"


def _asleep(url: str, error) -> str:
    """What to say when the pool never picked up — a sleeping box or a wrong address."""
    from beeagent.i18n import L
    return L(f"the pool at {url} did not answer even after waiting for it to wake up — "
             f"a free instance sleeps when nobody uses it, and a wrong address sleeps "
             f"forever ({error.__class__.__name__})",
             f"пул по адресу {url} не ответил, даже подождав его пробуждения — "
             f"бесплатный инстанс засыпает без обращений, а неверный адрес спит вечно "
             f"({error.__class__.__name__})")


def _headers(token: str) -> dict:
    from beeagent import __version__

    return {"Authorization": f"Bearer {token}", "User-Agent": f"beecode/{__version__}"}


def enroll(url: str, timeout: float = 15.0) -> dict:
    """Ask the pool for a seat. Returns its answer: token, budgets, approval."""
    endpoint = url.rstrip("/") + "/v1/enroll"
    with httpx.Client(timeout=timeout) as client:
        response = client.post(endpoint, json={})
    if response.status_code != 200:
        raise PoolError(_reason(response.status_code, _safe_json(response)))
    return _safe_json(response) or {}


def pool_status(url: str, token: str, timeout: float = 10.0) -> dict:
    """A small read-only peek: is the pool there, and does it know us?"""
    endpoint = url.rstrip("/") + "/healthz"
    with httpx.Client(timeout=timeout) as client:
        return _safe_json(client.get(endpoint)) or {}


def _safe_json(response) -> dict | None:
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError):
        return None
    return body if isinstance(body, dict) else None


class PoolProvider(BaseProvider):
    name = "pool"
    # The pool answers for whichever account keys it holds, so the list is the
    # provider's, not this file's; /pool status shows what is really there.
    models: list[str] = []

    def __init__(self, url: str = "", token: str = "", idle_timeout: float = 90.0):
        self.url = (url or "").rstrip("/")
        self.token = token or ""
        self.idle_timeout = idle_timeout

    @property
    def configured(self) -> bool:
        return bool(self.url and self.token)

    def _body(self, messages: list[dict], model: str, stream: bool) -> dict:
        return {"model": model, "messages": messages, "stream": stream}

    def _require(self) -> str:
        if not self.url:
            from beeagent.i18n import L
            raise PoolError(L("the pool has no address — /pool url https://…",
                              "у пула нет адреса — /pool url https://…"))
        if not self.token:
            from beeagent.i18n import L
            raise PoolError(L("no seat token yet — /pool enroll",
                              "нет токена места — /pool enroll"))
        return self.token

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        token = self._require()
        for attempt in (1, 2):
            try:
                return await self._chat(messages, model, token)
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                if attempt == 2:
                    raise PoolError(_asleep(self.url, e)) from e
                await asyncio.sleep(COLD_START_WAIT)
        return ""                       # unreachable: every path returns or raises

    async def _chat(self, messages: list[dict], model: str, token: str) -> str:
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.idle_timeout,
                                                           connect=TIMEOUT_CONNECT)) as client:
            response = await client.post(self.url + "/v1/chat/completions",
                                         json=self._body(messages, model, False),
                                         headers=_headers(token))
        if response.status_code != 200:
            raise PoolError(_reason(response.status_code, _safe_json(response)))
        body = _safe_json(response) or {}
        choices = body.get("choices") or []
        if not choices:
            return ""
        return str((choices[0].get("message") or {}).get("content") or "")

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator:
        token = self._require()
        for attempt in (1, 2):
            started = False
            try:
                async for pair in self._stream(messages, model, token):
                    started = True
                    yield pair
                return
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                # Only a connection that never opened is retried: once the first
                # fragment is on screen, repeating the request would print the
                # answer twice and pay for it twice.
                if started or attempt == 2:
                    raise PoolError(_asleep(self.url, e)) from e
                await asyncio.sleep(COLD_START_WAIT)

    async def _stream(self, messages: list[dict], model: str, token: str) -> AsyncIterator:
        timeout = httpx.Timeout(self.idle_timeout, connect=TIMEOUT_CONNECT)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url + "/v1/chat/completions",
                                     json=self._body(messages, model, True),
                                     headers=_headers(token)) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise PoolError(_reason(response.status_code, _safe_json(response)))
                got_any = False
                async for line in response.aiter_lines():
                    if (line or "").strip() in ("data: [DONE]", "data:[DONE]"):
                        break
                    piece = _chunk(line)
                    if piece is None:
                        continue
                    kind, text = piece
                    if text:
                        got_any = True
                        yield kind, text
                if not got_any:
                    raise PoolError("the pool streamed nothing")


def _chunk(line: str):
    """One SSE line of an OpenAI-shaped stream, or None.

    `reasoning_content` is kept because several pool providers think out loud in
    that field, and the agent shows it separately from the answer.
    """
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        event = json.loads(payload)
    except ValueError:
        return None
    error = event.get("error")
    if isinstance(error, str) and error:
        # The pool said why; do not turn that into "streamed nothing".
        raise PoolError(error)
    if isinstance(error, dict) and error.get("message"):
        raise PoolError(str(error["message"]))
    choices = event.get("choices") or []
    if not choices:
        return None
    delta = choices[0].get("delta") or {}
    content = delta.get("content")
    if isinstance(content, str) and content:
        return "content", content
    for field in ("reasoning", "reasoning_content", "thinking"):
        value = delta.get(field)
        if isinstance(value, str) and value:
            return "reasoning", value
    return None
