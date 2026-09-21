"""g4f provider with explicit keyless-provider fallback.

g4f's auto-routing can pick providers that require API keys (e.g. Puter.js).
We pin a list of known working keyless providers and try them in order,
falling back to the default auto-routing only as a last resort.

Streaming yields (kind, text) tuples:
  ("content",   str)  -- the answer itself
  ("reasoning", str)  -- thinking-block tokens (model-dependent)
"""
from typing import AsyncIterator
import inspect

from .base import BaseProvider


# Keyless providers, fastest and largest-prompt first. Measured 2026-09-21
# against g4f 8.5.7: LLM7 and CohereForAI answered a 4k-token prompt with the
# needle intact in under 2 seconds, Yqcloud answered in 7 and refused 4k as
# "too long", Cloudflare was reachable but slow on its own models.
#
# The previous list named Free2GPT, Blackbox and DDG — none of which exist in
# this g4f any more — so the "keyless fallback" had quietly shrunk to two
# providers before auto-routing took over and picked upstreams that demand a
# key. tests/test_providers.py fails if a name here stops resolving.
KEYLESS_PROVIDERS = ("LLM7", "CohereForAI_C4AI_Command", "Yqcloud", "Cloudflare")


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
    # Curated quick picks (verified keyless). The full catalog of every
    # working provider is available via discover_models().
    models = [
        "glm-4.7-flash", "glm-5.2",
        "gpt-4", "gpt-4o", "gpt-4o-mini", "gpt-4.1", "gpt-4.1-mini", "gpt-4.5",
        "gpt-oss-120b", "o4-mini",
        "deepseek-v3", "deepseek-r1", "deepseek-chat",
        "gemini-2.5-flash", "gemini-2.5-pro", "gemini-3.5-flash", "gemini-3.8-pro",
        "grok-3", "kimi-k2",
        "qwen-3-235b", "qwen-3-32b", "qwen-72b",
        "llama-3.1-70b", "llama-4-scout", "llama-4-maverick",
        "mistral-small-3.1-24b", "sonar",
        "claude-3.5-sonnet", "claude-3-haiku", "gemini-pro", "gpt-3.5-turbo",
    ]

    # Cache for the discovered cross-provider catalog.
    _discovered: list[str] | None = None

    @classmethod
    def discover_models(cls) -> list[str]:
        """Every model advertised by any working g4f provider.

        Pure offline scan of the installed g4f package (provider classes carry
        their model lists as attributes), so it is instant and cached. The
        curated `models` list comes first, discovered extras follow.
        """
        if cls._discovered is not None:
            return list(cls._discovered)
        found: list[str] = []
        seen = set(cls.models)
        found.extend(cls.models)
        try:
            import g4f.Provider as P
            for pr in P.__providers__:
                if not getattr(pr, "working", False):
                    continue
                for m in getattr(pr, "models", None) or []:
                    if isinstance(m, str) and m not in seen:
                        seen.add(m)
                        found.append(m)
        except Exception:
            pass
        cls._discovered = found
        return list(found)

    _upstream_map: dict[str, list[str]] | None = None

    @classmethod
    def upstream_map(cls) -> dict[str, list[str]]:
        """model -> the g4f providers that advertise it (offline, cached)."""
        if cls._upstream_map is not None:
            return cls._upstream_map
        mapping: dict[str, list[str]] = {}
        try:
            import g4f.Provider as P
            for pr in P.__providers__:
                if not getattr(pr, "working", False):
                    continue
                for model in getattr(pr, "models", None) or []:
                    if isinstance(model, str):
                        mapping.setdefault(model, []).append(pr.__name__)
        except Exception:
            pass
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
        provider_classes = _keyless_providers() + [None]  # None = default auto-routing
        last_error = None

        for cls in provider_classes:
            client = AsyncClient(provider=cls) if cls else AsyncClient()
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
        provider_classes = _keyless_providers() + [None]
        last_error = None

        for cls in provider_classes:
            client = AsyncClient(provider=cls) if cls else AsyncClient()
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
                # Swallowing this left the user staring at "empty response" while
                # the real cause (rate limit, dead endpoint) went unsaid.
                last_error = f"{getattr(cls, '__name__', 'auto')}: {e}"

        raise RuntimeError(f"g4f streamed nothing for '{model}': {last_error}")
