"""Two-language UI.

Every user-facing string is written once as a pair — English first, Russian
second — and `L()` picks according to the active language. Inline pairs (rather
than a key registry) keep the translation next to the code, so a new string
cannot ship without one.

Language comes from `beeagent.json` (`language`), the `--lang` flag or
`BEECODE_LANG`; `/lang` switches it at runtime.
"""
from __future__ import annotations

LANGUAGES = ("en", "ru")
DEFAULT = "en"

_lang = DEFAULT


def set_lang(code: str | None) -> str:
    """Activate a language; unknown codes fall back to English."""
    global _lang
    _lang = code if code in LANGUAGES else DEFAULT
    return _lang


def get_lang() -> str:
    return _lang


def L(english: str, russian: str) -> str:
    """The string in the active language."""
    return russian if _lang == "ru" else english
