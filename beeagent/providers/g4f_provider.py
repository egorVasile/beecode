"""g4f provider restricted to vetted keyless providers.

g4f's auto-routing is deliberately not used: it chooses from the whole installed
catalogue, and parts of that catalogue reach their endpoint by driving a headless
browser through a bot check. BeeCode must not ship that as a fallback, so when
the pinned providers below are exhausted this raises and the agent moves on to
the pool or a `/key` endpoint.

Streaming yields (kind, text) tuples:
  ("content",   str)  -- the answer itself
  ("reasoning", str)  -- thinking-block tokens (model-dependent)
"""
from typing import AsyncIterator
import inspect

from .base import BaseProvider


# Keyless providers, fastest and largest-prompt first. Measured 2026-09-21
# against g4f 8.5.7: LLM7 and CohereForAI answered a 4k-token prompt with the
# needle intact in under 2 seconds.
#
# Two names were dropped from the old list. "Cloudflare" is not an API client:
# g4f/Provider/Cloudflare.py:34 drives a headless Chrome at
# playground.ai.cloudflare.com through g4f/requests/cdp.py, which defines
# click_turnstile_checkbox() (:833) and bypass_turnstile() (:1080) — BeeCode
# must not ship a route that solves a bot check for its users. "Yqcloud" posts
# to api.binjie.fun with spoofed browser origin/referer/UA headers, an
# undocumented relay whose upstream model provenance nobody states.
#
# The previous list also named Free2GPT, Blackbox and DDG — none of which exist
# in this g4f any more — so the "keyless fallback" had quietly shrunk to two
# providers before auto-routing took over and picked upstreams that demand a
# key. tests/test_providers.py fails if a name here stops resolving.
KEYLESS_PROVIDERS = ("LLM7", "CohereForAI_C4AI_Command")


def _keyless_providers() -> list:
    """Resolve known keyless provider classes, skipping unavailable ones."""
    providers = []
    try:
        import g4f.Provider as P
        for name in KEYLESS_PROVIDERS:
            try:
                cls = getattr(P, name)
            except Exception:
                continue
            if getattr(cls, "working", False):
                providers.append(cls)
    except Exception:
        pass
    return providers


def _pinned_models() -> list[str]:
    """The model ids the pinned providers advertise, read off the installed package.

    Offline and instant — provider classes carry their lists as attributes — and
    the only catalogue BeeCode may claim, since a request now goes nowhere else.
    A name outside it is refused before it leaves the machine ("Model x not
    found" from CohereForAI) or answered 400 by LLM7 ("model_unavailable"):
    measured 2026-09-23, LLM7 rejected all 31 curated ids but its own `default`.
    """
    found: list[str] = []
    for cls in _keyless_providers():
        for model in getattr(cls, "models", None) or []:
            if isinstance(model, str) and model not in found:
                found.append(model)
    return found


# g4f's lists overstate what the endpoint behind them answers. Measured
# 2026-09-23 against CohereForAI's Hugging Face space, twice each: `command-r`
# and `command-r-plus` came back with an empty completion and
# `command-r7b-arabic-02-2025` stayed silent for 95 s. They are advertised, so
# they are excluded by name rather than left to be rediscovered as failures.
MEASURED_SILENT = ("command-r", "command-r-plus", "command-r7b-arabic-02-2025")


def _reasoning_text(delta) -> str:
    """Extract thinking tokens from a chunk delta, whatever the provider calls them."""
    for attr in ("reasoning", "reasoning_content", "reasoning_content_text", "thinking"):
        val = getattr(delta, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


async def _await_or_keep(response):
    """Await g4f's reply, unless it already handed back the stream itself.

    `AsyncClient.create(stream=True)` returns a coroutine for some providers and
    an async generator for others — auto-routing gives the generator. Awaiting
    that raised "object async_generator can't be used in 'await' expression",
    which failed every streamed answer and left the agent silently re-asking
    without tokens.
    """
    return await response if inspect.isawaitable(response) else response


class G4fProvider(BaseProvider):
    name = "g4f"
    # Every id here answered through the pinned path in one measured request
    # (2026-09-23, g4f 8.5.1, "Reply with exactly: BEE-OK", retried once). What
    # the old list carried — gemini, claude, gpt-4.x, kimi, qwen, llama,
    # deepseek, glm, grok, sonar — was served by g4f's auto-routing, and that
    # fallback is gone: asked today each of those names dies in g4f's model
    # lookup ("Model gemini-2.5-pro not found") or comes back
    # `400 model_unavailable` from llm7.io, which refuses every id but its own.
    #
    # Widest measured context first: `command-a-03-2025` is the route measured
    # holding a 32k prompt and the config default, the next three answer on the
    # same keyless Cohere space, and `default` is LLM7's only id — it will not
    # say which model is behind it, which is why it comes last.
    models = [
        "command-a-03-2025",
        "command-r-plus-08-2024",
        "command-r-08-2024",
        "command-r7b-12-2024",
        "default",
    ]

    # Cache for the catalog of the pinned providers.
    _discovered: list[str] | None = None

    @classmethod
    def discover_models(cls) -> list[str]:
        """Every model a pinned provider advertises and actually answers.

        There is nothing else to discover: a request goes to LLM7 or
        CohereForAI or nowhere, so scanning all of `g4f.Provider.__providers__`
        listed hundreds of names this provider cannot serve. Still offline and
        instant — the two classes carry their lists as attributes — and still
        excludes `MEASURED_SILENT`, the ids they advertise but answer with an
        empty completion or with silence.
        """
        if cls._discovered is not None:
            return list(cls._discovered)
        found: list[str] = []
        seen = set(cls.models)
        found.extend(cls.models)
        for model in _pinned_models():
            if model not in seen and model not in MEASURED_SILENT:
                seen.add(model)
                found.append(model)
        cls._discovered = found
        return list(found)

    _upstream_map: dict[str, list[str]] | None = None

    # A model is only worth recommending if it can hold a real working session:
    # the tool catalog, skills and file dumps spend tokens quickly.
    RECOMMENDED_MIN_TOKENS = 32768

    @classmethod
    def by_window(cls, models: list[str]) -> list[str]:
        """Order models by context window, biggest first.

        A window BeeCode measured itself leads a window a model id merely
        claims — but only while it is wide: measuring `gpt-4` at 2k says it is
        small, so it sinks to where 2k belongs instead of taking third place in
        a list about capacity. `/models` marks ✔ measured and ~ claimed.

        Each window is looked up once: sorting 600 names asked the same question
        600 times and showed it as a visible pause.
        """
        from beeagent.core import windows
        from beeagent.core.context import advertised_window

        sizes = {model: advertised_window(model) for model in set(models)}

        def rank(model: str):
            size = sizes[model]
            proven_wide = bool(windows.measured(model)) and size >= cls.RECOMMENDED_MIN_TOKENS
            return (0 if proven_wide else 1, -size, model)

        return sorted(models, key=rank)

    @classmethod
    def recommended_models(cls, limit: int = 40, models: list[str] = None) -> list[str]:
        """Catalog models that can carry a long session, largest window first.

        `models` accepts a list already ordered by `by_window`, so a caller that
        sorted the catalog for display does not sort it a second time.
        """
        from beeagent.core.context import advertised_window

        pool = cls.discover_models() if models is None else models
        wide = [m for m in pool if advertised_window(m) >= cls.RECOMMENDED_MIN_TOKENS]
        return cls.by_window(wide)[:limit]

    @classmethod
    def upstream_map(cls) -> dict[str, list[str]]:
        """model -> the pinned provider that advertises it (offline, cached).

        The `/models` table prints this under "served by", so scanning all of
        g4f here credited BlackboxPro, Cloudflare and the rest with serving
        models this provider can never ask them for. Only the two pinned
        classes answer, so only they are named.
        """
        if cls._upstream_map is not None:
            return cls._upstream_map
        mapping: dict[str, list[str]] = {}
        for pr in _keyless_providers():
            for model in getattr(pr, "models", None) or []:
                if isinstance(model, str):
                    mapping.setdefault(model, []).append(pr.__name__)
        cls._upstream_map = mapping
        return mapping

    @classmethod
    def upstreams(cls) -> list[str]:
        """Provider names usable as a /models filter, most models first."""
        counts: dict[str, int] = {}
        for providers in cls.upstream_map().values():
            for name in providers:
                counts[name] = counts.get(name, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Make history provider-safe.

        Free g4f backends reject non-standard history: role "tool" without
        tool_call_id, and custom tool_calls fields. We rewrite:
        - assistant messages: keep only plain text content
        - tool messages: fold into user messages ("[tool result] ...")
        - merge consecutive user messages to keep strict alternation
        """
        sanitized: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""

            if role == "system":
                sanitized.append({"role": "system", "content": content})
                continue

            if role == "assistant":
                # Drop custom tool_calls field; keep plain text only.
                text = content.strip()
                if text:
                    sanitized.append({"role": "assistant", "content": text})
                continue

            if role == "tool":
                text = f"[tool result]\n{content}".strip()
                if sanitized and sanitized[-1]["role"] == "user" and "tool result]" in sanitized[-1]["content"]:
                    sanitized[-1]["content"] += "\n\n" + text
                else:
                    sanitized.append({"role": "user", "content": text})
                continue

            # user (default)
            text = content.strip()
            if text:
                if sanitized and sanitized[-1]["role"] == "user":
                    sanitized[-1]["content"] += "\n\n" + text
                else:
                    sanitized.append({"role": "user", "content": text})

        return sanitized

    async def chat(self, messages: list[dict], model: str = "gpt-4", stream: bool = False) -> str:
        from g4f.client import AsyncClient

        messages = self._sanitize_messages(messages)
        provider_classes = _keyless_providers()
        last_error = None

        for cls in provider_classes:
            client = AsyncClient(provider=cls)
            try:
                response = await _await_or_keep(client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=stream,
                ))
                if stream:
                    parts = []
                    async for chunk in response:
                        if not chunk.choices:
                            continue
                        content = getattr(chunk.choices[0].delta, "content", None)
                        if content:
                            parts.append(content)
                    text = "".join(parts)
                else:
                    message = response.choices[0].message
                    text = getattr(message, "content", None) or ""
                    if not text.strip() and _reasoning_text(message):
                        # A reasoning-only reply means the model never answered;
                        # treat it as a failure so the next provider is tried.
                        raise ValueError("model returned reasoning only")
                if text and text.strip():
                    return text
                last_error = "empty response"
            except Exception as e:
                last_error = str(e)

        raise RuntimeError(f"g4f failed on all providers: {last_error}")

    async def chat_stream(self, messages: list[dict], model: str = "gpt-4") -> AsyncIterator:
        from g4f.client import AsyncClient

        messages = self._sanitize_messages(messages)
        provider_classes = _keyless_providers()
        last_error = None

        for cls in provider_classes:
            client = AsyncClient(provider=cls)
            try:
                stream = await _await_or_keep(client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                ))
                got_any = False
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        got_any = True
                        yield ("content", content)
                    reasoning = _reasoning_text(delta)
                    if reasoning:
                        yield ("reasoning", reasoning)
                if got_any:
                    return
                last_error = "empty stream"
                # empty stream -> try next provider
            except Exception as e:
                if got_any:
                    # Half an answer already reached the user. Falling through to
                    # the next provider here glued two different replies into one
                    # message — the caller resets the stream and asks again, which
                    # is the only honest recovery left.
                    raise
                # Swallowing this left the user staring at "empty response" while
                # the real cause (rate limit, dead endpoint) went unsaid.
                last_error = f"{getattr(cls, '__name__', 'auto')}: {e}"

        raise RuntimeError(f"g4f streamed nothing for '{model}': {last_error}")
