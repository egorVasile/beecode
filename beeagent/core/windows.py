"""How large a model's context window actually is.

g4f does not ship the number — its `Model` carries name, base_provider,
best_provider and long_name, and provider classes keep `max_tokens = None`
placeholders — so the only honest source left is the endpoint itself: send a
growing prompt until it refuses, and read the limit out of what it says.

Measured values are cached in `.beeagent/windows.json` and win over any guess.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

CACHE = Path(".beeagent") / "windows.json"

# Sizes tried while climbing, in tokens. Each step is one real request.
LADDER = (2048, 4096, 8192, 16384, 32768, 65536, 131072)

# What providers write when a prompt is too big. Ordered by how precisely the
# number names the ceiling: "200001 tokens > 128000 maximum" must give 128000,
# not the size we happened to send.
_LIMIT_PATTERNS = (
    r">\s*(\d{3,})\s*(?:tokens?\s*)?maximum",
    r"maximum(?: context)?(?: length)?(?: is|:)?\s*['\"]?(\d{3,})",
    r"(\d{3,})\s*(?:tokens?\s*)?maximum",
    r"context (?:window|length)[^0-9]{0,12}(\d{3,})",
    r"max(?:imum)?[_ ](?:input |prompt |request )?tokens\D{0,20}(\d{3,})",
    r"too many tokens[^0-9]{0,20}(\d{3,})",
    r"prompt is too long\D{0,20}(\d{3,})",
    r"exceeds the maximum\D{0,30}(\d{3,})",
)


def limit_from_error(text: str) -> int | None:
    """Pull the advertised token limit out of a provider's error message."""
    message = (text or "").lower()
    if not message:
        return None
    for pattern in _LIMIT_PATTERNS:
        match = re.search(pattern, message)
        if match:
            return int(match.group(1))
    return None


def load() -> dict:
    if not CACHE.exists():
        return {}
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def measured(model: str) -> int | None:
    value = load().get(model)
    return int(value) if isinstance(value, (int, float)) and value else None


def remember(model: str, tokens: int) -> None:
    data = load()
    data[model] = int(tokens)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except OSError:
        pass


def _filler(tokens: int) -> str:
    """Roughly `tokens` worth of text that no provider will want to answer."""
    from beeagent.utils.tokens import count_tokens

    unit = "контекстное окно проверяется на размер "
    block = unit * max(1, tokens // max(1, count_tokens(unit, "gpt-4")))
    while count_tokens(block, "gpt-4") < tokens:
        block += unit * 8
    return block


# Only these phrases mean "your prompt is too big". Anything else — rate limit,
# auth, timeout, a dead endpoint — says nothing about the window, and recording
# it as one is how a 2048 got written for a 4k model.
_OVERFLOW_HINT = re.compile(
    r"maximum context|context length|context window|window exceeded|prompt is too long"
    r"|too many tokens|exceeds the maximum|input length|request too large", re.I)


class ProbeResult:
    def __init__(self, window: int | None = None, note: str = ""):
        self.window = window
        self.note = note

    def __bool__(self) -> bool:
        return bool(self.window)


def _describe(error: Exception) -> str:
    """Something printable for any exception, including the empty ones.

    `asyncio.TimeoutError` carries no message at all, and a probe that reports
    "not a size problem: " with nothing after the colon teaches the user
    nothing about why the measurement stopped.
    """
    text = str(error).strip()
    return text or type(error).__name__


async def probe(model: str, provider, ceiling: int = LADDER[-1], timeout: int = 240,
                on_step=None) -> ProbeResult:
    """Climb the ladder until the endpoint refuses because of the size.

    A refusal that names its limit (the good case) is used directly. A refusal
    that only says "too long" still narrows the window: it sits between the last
    size that worked and the first that failed, so the lower one is kept. Any
    other error means the endpoint is unhealthy, not small, and is reported as
    such without recording anything.
    """
    accepted = 0
    for size in [step for step in LADDER if step <= ceiling]:
        messages = [{"role": "user", "content": _filler(size) + "\nОтветь одним словом: ok"}]
        if on_step:
            on_step(size, accepted)
        try:
            await asyncio.wait_for(provider.chat(messages, model=model), timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            # Our own patience ran out. A free upstream can spend minutes on a
            # big prompt it will answer perfectly well — the least useful thing
            # to do is call that a small window.
            return ProbeResult(
                None, f"{size} tokens went unanswered in {timeout}s — that is a slow "
                      f"endpoint, not a small window. Retry with a longer --timeout.")
        except Exception as e:
            message = _describe(e)
            named = limit_from_error(message)
            if named:
                remember(model, named)
                return ProbeResult(named, f"endpoint named the limit: {named}")
            if not _OVERFLOW_HINT.search(message):
                return ProbeResult(None, f"not a size problem: {message[:140]}")
            if accepted:
                remember(model, accepted)
                return ProbeResult(accepted, f"refused between {accepted} and {size} tokens")
            return ProbeResult(None, "the very first request was refused")
        accepted = size
    if not accepted:
        return ProbeResult(None, "nothing was tried")
    remember(model, accepted)
    return ProbeResult(accepted, f"no refusal up to {accepted} tokens (a ceiling, not a limit)")
