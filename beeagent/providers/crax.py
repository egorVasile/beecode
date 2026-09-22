"""crax-gpt — an OpenAI-compatible endpoint with two different limits.

Their API publishes two kinds of 429, and telling them apart is the whole value
of this provider, because the fix is opposite for each:

* a **per-IP** rate limit (40/min, burst 30) — another key does not help, the
  account was never the problem. Waiting, or leaving through another address, does;
* a **daily allowance** (`daily_limit_exceeded`) — waiting does not help until
  midnight. Another key, which is another account, does.

So the rotation only spends spare keys on the second kind, and on the first it
asks instead of pretending: wait out the seconds the endpoint named, or raise the
address with the user's own command. Nothing is switched on by itself — a command
is shown in full and runs only after the user pressed "yes".
"""
from __future__ import annotations

import json
import time
from typing import AsyncIterator, Callable, Optional

import httpx

from .base import BaseProvider

BASE_URL = "https://gpt.crax.lol/v1"
CONNECT_TIMEOUT = 10.0
KEY_COOLDOWN = 90.0           # a spent-for-the-day account is not retried this hour

# The same list the pool server keeps. An endpoint that also sells image, video
# and audio generation will answer for those models, and a coding agent has no
# use for the answer — but the request is still spent, and it is the kind of
# request an account owner gets reported for. So they are never even offered:
# the picker shows chat models, and the pool refuses anything else that arrives.
NON_CHAT = ("image", "video", "seedream", "dall-e", "whisper", "tts", "embedding",
            "moderation", "realtime")


def is_chat_model(model: str) -> bool:
    name = (model or "").lower()
    return not any(shape in name for shape in NON_CHAT)


class CraxError(RuntimeError):
    """Anything the provider could not answer by itself."""

    def __init__(self, kind: str, message: str, *, retry_after: int = 0):
        super().__init__(message)
        self.kind = kind          # "auth" | "ip" | "daily" | "network" | "other"
        self.retry_after = retry_after


def keys_from(value: str) -> list[str]:
    """One key, or several separated by commas — the usual shape of a key pool."""
    return [part.strip() for part in (value or "").split(",") if part.strip()]


def classify(status: int, body: dict, retry_after: int, raw: str = "") -> CraxError:
    """The provider's error, in our words, with the wait it told us about."""
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    code = str(error.get("type") or error.get("code") or "").lower()
    detail = str(error.get("message") or "").strip()
    if not detail and raw:
        # A gateway that answers 502 with a plain body used to leave the user
        # with "crax-gpt answered 502" and nothing to show support.
        detail = " ".join(raw.split())[:180]
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
    # The catalogue is read from the endpoint on demand (`/pool models`, /models);
    # asking for it on every start would be a request that is not a chat, and the
    # whole point of these keys is that only chat goes out.
    models = ("qwen3.8-max", "gpt-5-6-luna", "grok-code-fast-1", "deepseek-v4-flash")
    label = "crax-gpt"
    # Verified against the endpoint: it answers `finish_reason: "tool_calls"` and
    # accepts the OpenAI history shape back. So the tool call never has to be
    # written as JSON inside a sentence — which is where every parser bug came from.
    supports_tools = True

    def __init__(self, api_key: str = "", base_url: str = BASE_URL,
                 idle_timeout: float = 90.0, ask: Optional[Callable] = None):
        self.keys = keys_from(api_key)
        self.base_url = base_url.rstrip("/")
        self.idle_timeout = idle_timeout
        self.spent: dict[int, float] = {}      # key index -> retry not before
        # The REPL installs this: it is the only place allowed to ask a question.
        self.ask = ask

    # --- the native protocol ------------------------------------------------

    @staticmethod
    def to_openai_tools(schemas: list[dict], max_description: int = 240) -> list[dict]:
        """Our tool descriptions as OpenAI's, so the endpoint can call them.

        The descriptions are trimmed to their first clause on purpose. The full
        text is written for the old protocol, where the model had to be talked out
        of inventing `read_directory` and of putting a whole file in prose; the
        endpoint's front end rejects a tools request over roughly 6 KB, and all ten
        tools verbatim come to 5.1 KB before the user has typed anything.
        """
        out = []
        for schema in schemas or []:
            text = str(schema.get("description", ""))
            for stop in (". ", "; ", " — "):
                cut = text.find(stop)
                if 0 < cut < max_description:
                    text = text[:cut + 1]
                    break
            out.append({"type": "function", "function": {
                "name": schema.get("name", ""),
                "description": text[:max_description],
                "parameters": schema.get("parameters") or {"type": "object", "properties": {}},
            }})
        return out

    @staticmethod
    def to_openai_history(messages: list[dict]) -> list[dict]:
        """Session rows as the wire expects them, with matching call ids.

        BeeCode stores a call as `{"tool": …, "args": {…}}` and the result as a
        plain tool message. OpenAI wants ids that tie the two together, so they
        are assigned while walking — deterministically, because the order in the
        transcript is the order they happened in.
        """
        out: list[dict] = []
        pending: list[str] = []
        counter = 0
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content") or ""
            calls = message.get("tool_calls") or []
            if role == "tool":
                if pending:
                    call_id = pending.pop(0)
                else:
                    # A result with no call in front of it: say so in the text
                    # rather than inventing an id the endpoint will reject.
                    out.append({"role": "user", "content": f"[tool result] {content}"})
                    continue
                out.append({"role": "tool", "tool_call_id": call_id, "content": str(content)})
                continue
            if role == "assistant" and calls:
                counter += 1
                call_id = f"call_{counter:04d}"
                pending.append(call_id)
                out.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": [{
                        "id": call_id, "type": "function",
                        "function": {
                            "name": str(calls[0].get("tool", "")),
                            "arguments": json.dumps(calls[0].get("args") or {}, ensure_ascii=False),
                        },
                    }],
                })
                continue
            out.append({"role": role, "content": str(content)})
        return out

    async def complete(self, messages: list[dict], model: str = "", tools: list[dict] = None) -> dict:
        """One request, the whole answer: text and/or the calls the model made.

        Non-streaming on purpose. A tool call streamed in fragments has to be
        reassembled before it means anything, and the turn is not shown to the
        user until then anyway — so streaming would only add a second place where
        the arguments can come apart.
        """
        self._require_key()
        self._only_chat(model)
        body: dict = {"model": model or (self.models[0] if self.models else ""),
                      "messages": self.to_openai_history(messages),
                      # Without this the endpoint answers with SSE even when we
                      # asked for one object — measured, not assumed.
                      "stream": False}
        if tools:
            body["tools"] = self.to_openai_tools(tools)
            body["tool_choice"] = "auto"
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
                                                 json=body, headers=self._headers(key))
            except httpx.HTTPError as e:
                raise CraxError("network", f"crax-gpt is not reachable: {e}")
            if response.status_code != 200:
                error = classify(response.status_code, _json_or_empty(response.text),
                                 _retry_after(response), response.text)
                await self._handle_limit(index, error)
                continue
            payload = _json_or_empty(response.text)
            choices = payload.get("choices") or []
            message = (choices[0] if choices else {}).get("message") or {}
            calls = []
            for call in message.get("tool_calls") or []:
                function = call.get("function") or {}
                try:
                    args = json.loads(function.get("arguments") or "{}")
                except ValueError:
                    args = {}
                calls.append({"tool": function.get("name", ""), "args": args if isinstance(args, dict) else {}})
            return {"text": str(message.get("content") or ""), "tool_calls": calls,
                    "usage": payload.get("usage") or {}, "key": index}
        raise CraxError("other", "crax-gpt refused every configured key")

    # --- key bookkeeping ----------------------------------------------------

    def _usable(self) -> list[tuple[int, str]]:
        now = time.time()
        return [(i, key) for i, key in enumerate(self.keys) if self.spent.get(i, 0) <= now]

    def _cool(self, index: int, seconds: float) -> None:
        # Bounded by a day, not by our own cooldown constant: an endpoint that
        # names a reset time knows better than we do.
        self.spent[index] = time.time() + max(1.0, min(seconds, 24 * 3600))

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
            # Never shorter than the endpoint's own stated reset: retrying a
            # spent account an hour before midnight just spends the attempt.
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
            raise CraxError("ip", L(
                f"crax-gpt is limiting this IP — wait {error.retry_after or 60}s",
                f"crax-gpt ограничил этот IP — подожди {error.retry_after or 60} с"),
                retry_after=error.retry_after or 60)

        raise error

    # --- the wire -----------------------------------------------------------

    def _only_chat(self, model: str) -> None:
        """Refuse a generation model before the request, not after it.

        The endpoint sells image, video and audio models on the same keys, and an
        account used for them is the kind of activity that gets reported. A
        refused request costs nothing; a sent one costs the account.
        """
        if not is_chat_model(model):
            from beeagent.i18n import L
            raise CraxError("other", L(f"“{model}” is not a chat model — BeeCode only "
                                       f"asks crax-gpt for text answers. Pick one with /model",
                                       f"«{model}» — не чат-модель. BeeCode просит у crax-gpt "
                                       "только текстовые ответы, выбери модель через /model"))

    def _payload(self, messages: list[dict], model: str, stream: bool) -> dict:
        self._only_chat(model)
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
                             _retry_after(response), response.text)
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
                            if _is_done(line):
                                break
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
                    # The answer stopped mid-sentence. Handing it back as if it
                    # were finished is how a half-written file gets reported as
                    # written — the caller retries, and only the retry knows.
                    raise CraxError("network",
                                    f"crax-gpt stopped after {len(''.join(pieces))} characters: {e}")
                raise CraxError("network", f"crax-gpt is not reachable: {e}")
            if got_any:
                return
        raise CraxError("other", "crax-gpt streamed nothing")

    def _timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self.idle_timeout, connect=CONNECT_TIMEOUT)

    def discover_models(self) -> list[str]:       # type: ignore[override]
        """The live catalogue; each entry carries its context length too.

        Filtered to chat: the endpoint lists its image and video models here, and
        an agent that offers them spends requests on answers it cannot use.
        """
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
            if isinstance(item, dict) and item.get("id") and is_chat_model(str(item["id"])):
                found.append(str(item["id"]))
        return found or list(self.models)


def _retry_after(response) -> int:
    """Seconds, from either shape the header is allowed to take."""
    raw = (response.headers.get("Retry-After") or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(float(raw)))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0, int((when - datetime.now(timezone.utc)).total_seconds()))
    except (TypeError, ValueError):
        return 0


def _is_done(line: str) -> bool:
    return (line or "").strip() in ("data: [DONE]", "data:[DONE]")


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
