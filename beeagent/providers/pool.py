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
from pathlib import Path
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
# Measured on the live pool: waking a slept instance took 22.6 s for an endpoint
# that does no work at all. Enrolment used to allow 15, which made the first
# `/pool enroll` of the day the one call that could not wait for the box to wake.
ENROLL_TIMEOUT = 90.0


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


def install_key():
    """This machine's signing key: made once, kept in `~/.beecode`, never sent.

    Per machine rather than per project on purpose — BeeCode keeps its settings in
    the folder it runs from, and an install key per folder would mean a new seat
    for every project the same person opens, which is exactly what the daily seat
    cap exists to limit.
    """
    import os
    import secrets

    from beeagent.utils import ed25519

    override = os.environ.get("BEECODE_POOL_KEY_FILE") or ""
    path = Path(override) if override else Path.home() / ".beecode" / "pool-key.json"
    seed = None
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            raw = bytes.fromhex(str(stored.get("seed") or ""))
            seed = raw if len(raw) == 32 else None
        except (OSError, ValueError):
            seed = None
    if seed is None:
        seed = secrets.token_bytes(32)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"seed": seed.hex()}), encoding="utf-8")
        try:
            path.chmod(0o600)
        except OSError:
            pass                      # Windows has no file bits to set
    public = ed25519.public_key(seed)
    return seed, public.hex(), public[:8].hex()


def signed_headers(seed: bytes, public_hex: str, body: bytes) -> dict:
    """Prove the request came from this install, without proving anything twice.

    The signature covers the timestamp, a fresh nonce and the bytes of the body:
    a captured request therefore dies in two minutes, and one captured inside that
    window cannot be sent a second time.
    """
    import hashlib
    import secrets
    import time

    from beeagent.utils import ed25519

    stamp = str(time.time())
    nonce = secrets.token_hex(16)
    digest = hashlib.sha256(body).hexdigest()
    signature = ed25519.sign(seed, f"{stamp}\n{nonce}\n{digest}".encode())
    return {"X-Seat-Timestamp": stamp, "X-Seat-Nonce": nonce,
            "X-Seat-Signature": signature.hex()}


def enroll(url: str, timeout: float = ENROLL_TIMEOUT) -> dict:
    """Ask the pool for a seat, naming this install as the one that owns it.

    Only the public half goes out, and only this once; the pool keeps it and
    refuses every later request that cannot sign with the key beside it.
    """
    seed, public, device = install_key()
    endpoint = url.rstrip("/") + "/v1/enroll"
    body = json.dumps({"device": device, "public_key": public}).encode()
    with httpx.Client(timeout=timeout) as client:
        response = client.post(endpoint, content=body,
                               headers={"Content-Type": "application/json"})
    if response.status_code != 200:
        raise PoolError(_reason(response.status_code, _safe_json(response)))
    return _safe_json(response) or {}


def pool_status(url: str, token: str, timeout: float = ENROLL_TIMEOUT) -> dict:
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
    # What the pool answers for, measured on the box itself 2026-09-24: every crax
    # account's chat models, ordered by how long a six-word reply took. `/models`
    # still asks the pool for the live list -- this is what the picker shows when it
    # cannot be reached, because an empty list reads as "the pool has no models"
    # and sends a person to change provider when the box was only asleep.
    models: list[str] = ["qwen3-coder-480b", "gemma-3-12b", "llama-4-maverick",
                         "gpt-5-6-luna", "kimi-k2-6", "glm-5.2", "grok-4-3",
                         "glm-5.3-flash", "deepseek-v4-flash", "kimi-k2-7-code",
                         "glm-5.3", "grok-4-6"]

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

    def _signed(self, body: dict) -> tuple[bytes, dict]:
        """The bytes to send and the headers that vouch for them.

        Sent as bytes rather than as `json=`: httpx would serialise the payload
        itself, and a signature over a different spelling of the same object is
        not a signature.
        """
        seed, public, _device = install_key()
        raw = json.dumps(body).encode()
        headers = _headers(self._require()) | signed_headers(seed, public, raw)
        headers["Content-Type"] = "application/json"
        return raw, headers

    async def _chat(self, messages: list[dict], model: str, token: str) -> str:
        raw, headers = self._signed(self._body(messages, model, False))
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.idle_timeout,
                                                           connect=TIMEOUT_CONNECT)) as client:
            response = await client.post(self.url + "/v1/chat/completions", content=raw,
                                         headers=headers)
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
        raw, headers = self._signed(self._body(messages, model, True))
        timeout = httpx.Timeout(self.idle_timeout, connect=TIMEOUT_CONNECT)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", self.url + "/v1/chat/completions",
                                     content=raw, headers=headers) as response:
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


    def discover_models(self) -> list[str]:
        """What the pool's accounts can answer for.

        The client often cannot ask the provider itself: crax's Cloudflare blocks
        some networks outright (error 1010, on the model list as much as on a
        chat), while the pool's own egress gets through. So the seat asks the
        pool, and gets only the chat models — the same rule the pool enforces on
        every request it forwards.
        """
        token = self._require()
        with httpx.Client(timeout=TIMEOUT_CONNECT + 10) as client:
            response = client.get(self.url + "/v1/models", headers=_headers(token))
        if response.status_code != 200:
            raise PoolError(_reason(response.status_code, _safe_json(response)))
        body = _safe_json(response) or {}
        return [str(item.get("id")) for item in body.get("data") or [] if item.get("id")]


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
