"""Token counts, exact when tiktoken is here and never optimistic when it is not.

Two rules hold this file together.

* When cl100k is available it decides every number. It is the tokenizer half
  these endpoints use, and an estimate next to a measurement is a guess.
* When it is not — Termux has no wheel and no compiler to build one — the
  fallback may over-count, because an over-count trims the history early while
  an under-count sends an oversized request the endpoint then trims behind our
  back, and the model answers as if the chat had just started.

The second rule is why the fallback is not one flat `chars * 0.6`. That number
is a safe bet for Latin and Cyrillic prose and a bad one for everything the
terminal actually paints: measured against cl100k_base on 2026-09-24 a flat 0.6
read an emoji at 0.23x its cost, a CJK glyph at 0.53x and the box-drawing
characters of the `diagram` tool's own output at 0.86x — so a request we were
sure fitted did not.
"""
import math
import re

_CACHE = {}

# Ranges whose tokens-per-character is above the Latin/Cyrillic norm, stripped
# out of the text in order — the ranges are disjoint, so the order only matters
# for readability. Every weight is at or above the worst measured price of the
# class, because the one thing this estimate may not do is under-read.
#
# Measured against cl100k_base on 2026-09-24 (tests/test_context_damage.py
# re-prices them on the samples, and fails if any of them stops holding):
# an emoji 2.3-3.0, a box-drawing corner 2.0 and a run of them 0.13, a CJK
# glyph 0.7-1.0, a Cyrillic character 0.5, a Latin one 0.14-0.34.
#
# 3.0 for pictographs, flags, dingbat-adjacent
#     symbols, plus the variation selector and ZWJ that join them
# 2.0 for arrows, box drawing, block elements,
#     geometric shapes, misc symbols and braille
# 1.6 for CJK, kana and Hangul
_HEAVY_CLASSES = (
    (re.compile("[\U0001F000-\U0001FAFF\U00002B00-\U00002BFF\U0000FE00-\U0000FE0F"
                "\U0000200D\U0001F1E6-\U0001F1FF]"), 3.0),
    (re.compile("[\U00003000-\U0000303F\U00003040-\U000030FF\U00003100-\U0000312F"
                "\U00003400-\U00004DBF\U00004E00-\U00009FFF\U0000AC00-\U0000D7AF"
                "\U0000F900-\U0000FAFF]"), 1.6),
    (re.compile("[\U00002190-\U000021FF\U00002500-\U0000257F\U00002580-\U0000259F"
                "\U000025A0-\U000025FF\U00002600-\U000027BF\U00002800-\U000028FF]"), 2.0),
)

# Everything left — Latin letters, digits, punctuation, whitespace, and the
# scripts that cost about the same as Cyrillic. Real size for English prose is
# ~0.24 and for Russian ~0.43, so this stays a deliberate over-count: with no
# tokenizer there is no way to tell a cheap string from an expensive one, and
# the two directions do not cost the same.
_DEFAULT_WEIGHT = 0.6


def _encoding(model: str):
    """The tokenizer to budget with.

    `encoding_for_model` raises KeyError for every non-OpenAI id, which is the
    whole g4f catalogue, so an unknown model falls back to cl100k_base — a real
    tokenizer — rather than to a char count that under-reads Cyrillic by ~2x.
    """
    import tiktoken

    name = (model or "").lower()
    if name not in _CACHE:
        try:
            _CACHE[name] = tiktoken.encoding_for_model(model)
        except Exception:
            _CACHE[name] = tiktoken.get_encoding("cl100k_base")
    return _CACHE[name]


def rough_count(text: str) -> int:
    """The no-tiktoken estimate: weighted by what kind of characters these are.

    Public because the tests price it against the real tokenizer, which is the
    only way to show it errs on the safe side without an Android device.
    """
    left = text
    total = 0.0
    for pattern, weight in _HEAVY_CLASSES:
        rest = pattern.sub("", left)
        total += (len(left) - len(rest)) * weight
        left = rest
    return max(1, math.ceil(len(left) * _DEFAULT_WEIGHT + total))


def count_tokens(text: str, model: str = "gpt-4") -> int:
    """Tokens this text will cost the model.

    Getting this wrong in the optimistic direction is what made small-context
    models forget the conversation: we thought the request fit, the endpoint
    trimmed it and the history never reached the model.
    """
    if not text:
        return 0
    try:
        return len(_encoding(model).encode(text))
    except Exception:
        # tiktoken is optional — Android has no wheel for it and no compiler to
        # build one. Without it, over-count rather than send an oversized request.
        return rough_count(text)


def cut_tokens(text: str, tokens: int, model: str = "gpt-4", head: float = 0.7) -> tuple[str, str, int]:
    """Split `text` into (head, tail, removed_tokens) costing about `tokens` in total.

    Cutting on real token boundaries rather than a chars/4 guess: Cyrillic
    costs roughly twice the tokens that estimate allows, so a char-based clip
    kept sending messages that were still too big.
    """
    if tokens <= 0:
        return "", "", count_tokens(text, model)
    try:
        enc = _encoding(model)
        ids = enc.encode(text)
    except Exception:
        total = count_tokens(text, model)
        if total <= tokens:
            return text, "", 0
        # No tokenizer: size the cut off this text's own density rather than a
        # constant — an emoji or a box-drawing run costs several times what the
        # same number of Latin characters does — and split what is kept between
        # head and tail, so the two together are the budget and not twice it.
        density = total / max(1, len(text))
        chars_per_token = 1.0 / max(_DEFAULT_WEIGHT, density)
        keep_head = max(1, int(tokens * head * chars_per_token))
        keep_tail = max(1, int(tokens * (1 - head) * chars_per_token))
        return text[:keep_head], text[-keep_tail:], max(0, total - tokens)
    if len(ids) <= tokens:
        return text, "", 0
    keep_head = max(1, int(tokens * head))
    keep_tail = max(1, tokens - keep_head)
    return (enc.decode(ids[:keep_head]), enc.decode(ids[-keep_tail:]),
            max(0, len(ids) - keep_head - keep_tail))
