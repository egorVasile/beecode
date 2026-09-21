"""How large a model's context window actually is.

g4f does not ship the number — its `Model` carries name, base_provider,
best_provider and long_name, and provider classes keep `max_tokens = None`
placeholders — so the only honest source left is the endpoint itself. Two
signals are read off it: how large a prompt it refuses, and how large a prompt
the model still *saw* — free endpoints commonly trim the request behind our
back and answer as if the chat had just started, which no error would reveal.

Measured values are cached in `.beeagent/windows.json` and win over any guess.
"""
from __future__ import annotations

import asyncio
import json
import re
import secrets
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


_CACHED: dict | None = None
_CACHED_KEY: tuple | None = None


def load() -> dict:
    """The measured windows, held in memory until the file actually changes.

    A model list asks for one window per model — 600+ times per screen — and
    re-reading the JSON each time was measured at over half a second of
    `/models`. The file's identity (path, mtime, size) is the cache key, so a
    test pointing CACHE elsewhere, or a probe writing new numbers, is picked up
    without anyone having to remember to invalidate.
    """
    global _CACHED, _CACHED_KEY
    try:
        stat = CACHE.stat()
        key = (str(CACHE), stat.st_mtime_ns, stat.st_size)
    except OSError:
        _CACHED, _CACHED_KEY = {}, None
        return _CACHED
    if _CACHED is not None and key == _CACHED_KEY:
        return _CACHED
    try:
        data = json.loads(CACHE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        data = {}
    _CACHED, _CACHED_KEY = (data if isinstance(data, dict) else {}), key
    return _CACHED


def measured(model: str) -> int | None:
    value = load().get(model)
    return int(value) if isinstance(value, (int, float)) and value else None


def remember(model: str, tokens: int) -> None:
    data = dict(load())
    data[model] = int(tokens)
    try:
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        _CACHED[model] = int(tokens)     # keep the in-memory copy in step
    except OSError:
        pass


def _filler(tokens: int, needle: str = "") -> str:
    """Roughly `tokens` worth of text that no provider will want to answer."""
    from beeagent.utils.tokens import count_tokens

    unit = "контекстное окно проверяется на размер "
    block = unit * max(1, tokens // max(1, count_tokens(unit, "gpt-4")))
    while count_tokens(block, "gpt-4") < tokens:
        block += unit * 8
    return f"{needle}\n{block}" if needle else block


def _new_needle() -> str:
    return "BEE-" + secrets.token_hex(3).upper()


RECALL_ASK = ("\n\nВ самом начале этого сообщения стоит код вида BEE-XXXXXX. "
              "Напиши ровно этот код и больше ничего.")
PLAIN_ASK = "\nОтветь одним словом: ok"


# Only these phrases mean "your prompt is too big". Anything else — rate limit,
# auth, timeout, a dead endpoint — says nothing about the window, and recording
# it as one is how a 2048 got written for a 4k model.
_OVERFLOW_HINT = re.compile(
    r"maximum context|context length|context window|window exceeded|prompt is too long"
    r"|too many tokens|exceeds the maximum|input length|request too large"
    r"|文字过长|请输入更短", re.I)   # Yqcloud answers 429 with this in Chinese


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


async def _send(provider, model: str, content: str, timeout: int, attempts: int):
    """One probe request, retried while the failure says nothing about size.

    A free endpoint drops ordinary requests constantly — 401 from a guest
    upstream, a rotated provider that needs a key, a quota error — and one such
    hiccup must not end a measurement that is three steps in. A real refusal is
    not retried: it is the answer we came for. A step that ran out of patience
    is not retried either — it already cost `timeout` seconds, and repeating
    that would only make the honest conclusion arrive later.
    """
    error = None
    for _ in range(max(1, attempts)):
        try:
            return "ok", await asyncio.wait_for(
                provider.chat([{"role": "user", "content": content}], model=model), timeout)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as e:
            return "timeout", e
        except Exception as e:
            error = e
            message = _describe(e)
            if limit_from_error(message) or _OVERFLOW_HINT.search(message):
                return "error", e
    return "error", error


async def probe(model: str, provider, ceiling: int = LADDER[-1], timeout: int = 240,
                attempts: int = 3, on_step=None) -> ProbeResult:
    """Climb the ladder until the endpoint stops really taking the prompt.

    Two ways to fail are measured. The endpoint may refuse, ideally naming its
    limit (the good case); an unhelpful "too long" still narrows the window
    between the last size that fitted and the first that did not. Or it may
    accept the request and quietly drop the front of it — caught by burying a
    random code at the very start and asking for it back, so a prompt the model
    never saw cannot be counted as fitting. An endpoint that cannot do that at
    the smallest size is not being dishonest, just dim, and falls back to the
    refusal signal alone.
    """
    accepted = 0
    reads_it_back = True
    for size in [step for step in LADDER if step <= ceiling]:
        needle = _new_needle() if reads_it_back else ""
        content = _filler(size, needle) + (RECALL_ASK if needle else PLAIN_ASK)
        if on_step:
            on_step(size, accepted)
        kind, value = await _send(provider, model, content, timeout, attempts)
        if kind == "timeout":
            # Our own patience ran out. A free upstream can spend minutes on a
            # big prompt it will answer perfectly well — the least useful thing
            # to do is call that a small window.
            return ProbeResult(
                None, f"{size} tokens went unanswered in {timeout}s — that is a slow "
                      f"endpoint, not a small window. Retry with a longer --timeout.")
        if kind == "error":
            message = _describe(value)
            named = limit_from_error(message)
            if named:
                remember(model, named)
                return ProbeResult(named, f"endpoint named the limit: {named}")
            if not _OVERFLOW_HINT.search(message):
                return ProbeResult(None, f"not a size problem after {attempts} tries: "
                                         f"{message[:140]}")
            if accepted:
                remember(model, accepted)
                return ProbeResult(accepted, f"refused between {accepted} and {size} tokens")
            return ProbeResult(None, "the very first request was refused")
        reply = value

        if needle and needle.lower() not in (reply or "").lower():
            if not accepted:
                # It failed the trick at the smallest size, so the trick tells us
                # nothing here; keep climbing on refusals alone.
                reads_it_back = False
                accepted = size
                continue
            remember(model, accepted)
            return ProbeResult(
                accepted, f"read it, forgot it: at {size} tokens the model no longer "
                          f"repeats the code at the start of the prompt")
        accepted = size
    if not accepted:
        return ProbeResult(None, "nothing was tried")
    remember(model, accepted)
    seen = "the model still recalled the prompt" if reads_it_back else "no refusal"
    return ProbeResult(accepted, f"{seen} up to {accepted} tokens (a ceiling, not a limit)")
