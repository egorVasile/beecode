"""crax-gpt — an OpenAI-compatible endpoint with two different limits.

Their API publishes two kinds of 429, and telling them apart is the whole value
of this provider, because the fix is opposite for each:

* a **per-IP** rate limit (40/min, burst 30) — another key does not help, the
  account was never the problem. Waiting, or leaving through another address, does;
* a **daily allowance** (`daily_limit_exceeded`) — waiting does not help until
  midnight. Another key, which is another account, does.

So the rotation only spends spare keys on the second kind, and on the first it
asks instead of pretending: wait, or take the route through the pool relay if the
user configured one. Nothing is switched on by itself — a VPN command is shown in
full and runs only after the user pressed "yes".
"""
from __future__ import annotations

import time
from typing import AsyncIterator, Callable, Optional

import httpx

from .base import BaseProvider

BASE_URL = "https://gpt.crax.lol/v1"
CONNECT_TIMEOUT = 10.0
KEY_COOLDOWN = 90.0           # a spent-for-the-day account is not retried this hour


class CraxError(RuntimeError):
    """Anything the provider could not answer by itself."""

    def __init__(self, kind: str, message: str, *, retry_after: int = 0):
        super().__init__(message)
        self.kind = kind          # "auth" | "ip" | "daily" | "network" | "other"
        self.retry_after = retry_after


def keys_from(value: str) -> list[str]:
    """One key, or several separated by commas — the usual shape of a key pool."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def classify(status: int, body: dict, retry_after: int) -> CraxError:
    """The provider's error, in our words, with the wait it told us about."""
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("type") or error.get("code") or "").lower()
    detail = str(error.get("message") or "").strip()
    if status == 401 or "auth" in code:
        # The endpoint's own words are "log in at the site" — useless to someone
        # holding a key. Say where the key goes.
        from beeagent.i18n import L
        return CraxError("auth", (detail or "crax-gpt did not accept this key")
                         + " — " + L("put your own key in: /key crax crk_live_…",
                                     "впиши свой ключ: /key crax crk_live_…"))
    if status == 429:
        if "daily" in code or "daily" in detail.lower():
            return CraxError("daily", detail or "this account's daily allowance is spent",
                             retry_after=retry_after)
        return CraxError("ip", detail or "crax-gpt is limiting this IP address",
                         retry_after=retry_after)
    return CraxError("other", detail or f"crax-gpt answered {status}", retry_after=retry_after)


class CraxProvider(BaseProvider):
    name = "crax"
    models = ("qwen3.8-max",)
    label = "crax-gpt"

    def __init__(self, api_key: str = "", base_url: str = BASE_URL,
                 idle_timeout: float = 90.0, ask: Optional[Callable] = None):
        self.keys = keys_from(api_key)
        self.base_url = base_url.rstrip("/")
        self.idle_timeout = idle_timeout
        self.spent: dict[int, float] = {}      # key index -> retry not before
        # The REPL installs this: it is the only place allowed to ask a question.
        self.ask = ask

    # --- key bookkeeping ----------------------------------------------------

    def _usable(self) -> list[tuple[int, str]]:
        now = time.time()
        return [(i, key) for i, key in enumerate(self.keys) if self.spent.get(i, 0) <= now]

    def _cool(self, index: int, seconds: float) -> None:
        self.spent[index] = time.time() + max(1.0, min(seconds, KEY_COOLDOWN * 20))

    def _headers(self, key: str) -> dict:
        from beeagent import __version__

        return {"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                "User-Agent": f"beecode/{__version__}"}

    def _require_key(self) -> None:
        if not self.keys:
            from beeagent.i18n import L
            raise CraxError("auth", L("no crax-gpt key — /key crax crk_live_…",
                                      "нет ключа crax-gpt — /key crax crk_live_…"))

    # --- the two limits, told apart ----------------------------------------

    async def _handle_limit(self, index: int, error: CraxError) -> None:
        """Decide what a 429 means for this key, and what to do about it.

        Raises so the caller stops; returns only when the user chose something
        that can actually be honoured.
        """
        from beeagent.i18n import L

        if error.kind == "daily":
            self._cool(index, max(KEY_COOLDOWN, error.retry_after or KEY_COOLDOWN))
            if self._usable():
                return                       # the next key is another account: try it
            raise CraxError("daily", L(
                f"every crax-gpt key in the config has spent its day — {error.retry_after}s "
                "until the allowance resets, or add another key with /key crax …",
                f"все ключи crax-gpt выбрали дневную норму — сброс через {error.retry_after} с, "
                "или добавь ещё ключ: /key crax …"), retry_after=error.retry_after)

        if error.kind == "ip":
            answer = None
            if self.ask is not None:
                answer = await self.ask(error)
            if answer == "wait":
                await _sleep(error.retry_after or 5)
                return
            if answer == "relay":
                raise CraxError("relay", "route this request through the pool", retry_after=0)
            raise CraxError("ip", L(
                f"crax-gpt is limiting this IP — wait {error.retry_after or 60}s",
                f"crax-gpt ограничил этот IP — подожди {error.retry_after or 60} с"),
                retry_after=error.retry_after or 60)

        raise error

    # --- the wire -----------------------------------------------------------

    def _payload(self, messages: list[dict], model: str, stream: bool) -> dict:
        return {"model": model or (self.models[0] if self.models else ""),
                "messages": messages, "stream": stream, "include_reasoning": True}

    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        self._require_key()
        attempts = 0
        while attempts < max(1, len(self.keys)):
            usable = self._usable()
            if not usable:
                raise CraxError("daily", "every configured key is cooling down")
            index, key = usable[0]
            attempts += 1
            try:
                async with httpx.AsyncClient(timeout=self._timeout()) as client:
                    response = await client.post(self.base_url + "/chat/completions",
                                                 json=self._payload(messages, model, False),
                                                 headers=self._headers(key))
            except httpx.HTTPError as e:
                raise CraxError("network", f"crax-gpt is not reachable: {e}")
            if response.status_code == 200:
                body = _json_or_empty(response.text)
                choices = body.get("choices") or []
                return str((choices[0].get("message") or {}).get("content") or "") if choices else ""
            error = classify(response.status_code, _json_or_empty(response.text),
                             _retry_after(response))
            await self._handle_limit(index, error)
        raise CraxError("other", "crax-gpt refused every configured key")

    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator:
        self._require_key()
        attempts = 0
        while attempts < max(1, len(self.keys)):
            usable = self._usable()
            if not usable:
                raise CraxError("daily", "every configured key is cooling down")
            index, key = usable[0]
            attempts += 1
            got_any = False
            pieces: list[str] = []
            try:
                async with httpx.AsyncClient(timeout=self._timeout()) as client:
                    async with client.stream("POST", self.base_url + "/chat/completions",
                                             json=self._payload(messages, model, True),
                                             headers=self._headers(key)) as response:
                        if response.status_code != 200:
                            await response.aread()
                            error = classify(response.status_code,
                                             _json_or_empty(response.text), _retry_after(response))
                            await self._handle_limit(index, error)
                            continue
                        async for line in response.aiter_lines():
                            piece = _chunk(line)
                            if piece is None:
                                continue
                            kind, text = piece
                            if not text:
                                continue
                            got_any = True
                            pieces.append(text)
                            yield kind, text
            except httpx.HTTPError as e:
                if got_any:
                    return              # half an answer reached the user already
                raise CraxError("network", f"crax-gpt is not reachable: {e}")
            if got_any:
                return
        raise CraxError("other", "crax-gpt streamed nothing")

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.idle_timeout, connect=CONNECT_TIMEOUT)

    def discover_models(self) -> list[str]:       # type: ignore[override]
        """The live catalogue; each entry carries its context length too."""
        if not self.keys:
            return list(self.models)
        try:
            with httpx.Client(timeout=15.0) as client:
                response = client.get(self.base_url + "/models", headers=self._headers(self.keys[0]))
            body = response.json()
        except Exception:
            return list(self.models)
        found = []
        for item in body.get("data") or body.get("models") or []:
            if isinstance(item, dict) and item.get("id"):
                found.append(str(item["id"]))
        return found or list(self.models)


def _retry_after(response) -> int:
    try:
        return max(0, int(float(response.headers.get("Retry-After") or 0)))
    except (TypeError, ValueError):
        return 0


def _json_or_empty(text: str) -> dict:
    import json

    try:
        body = json.loads(text or "{}")
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _chunk(line: str):
    line = (line or "").strip()
    if not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    import json

    try:
        event = json.loads(payload)
    except ValueError:
        return None
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


async def _sleep(seconds: float) -> None:
    import asyncio

    await asyncio.sleep(max(0.0, min(float(seconds or 0), 300.0)))
