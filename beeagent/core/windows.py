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


async def probe(model: str, provider, ceiling: int = LADDER[-1], timeout: int = 120,
                on_step=None) -> int | None:
    """Climb the ladder until the endpoint refuses; the last accepted size wins.

    A refusal that names its limit (the good case) is used directly. A refusal
    that does not is still informative: the window sits between the last size
    that worked and the first that failed, so we keep the lower one.
    """
    accepted = 0
    for size in [step for step in LADDER if step <= ceiling]:
        messages = [{"role": "user", "content": _filler(size) + "\nОтветь одним словом: ok"}]
        if on_step:
            on_step(size, accepted)
        try:
            await asyncio.wait_for(provider.chat(messages, model=model), timeout)
        except Exception as e:
            named = limit_from_error(str(e))
            if named:
                result = min(named, max(accepted, named))
            else:
                result = accepted
            if result:
                remember(model, result)
            return result or None
        accepted = size
    if accepted:
        remember(model, accepted)
    return accepted or None
