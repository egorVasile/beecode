"""Swappable pieces of the interface.

Everything a user might want to remove — the frame around a panel, the shimmering
banner, the playful "wiping honey off the keyboard..." line — is looked up here
instead of being hardcoded at the call site. A slot holds a name ("rounded",
"none"), and each slot has a small registry so a plugin can add its own variant
rather than replace the built-in one.

`none` is a real value, not a special case in the caller: it returns kwargs that
rich understands as "draw this without a box", which is why turning the frame off
does not need a fork of the UI.
"""
from __future__ import annotations

from rich import box

from beeagent.i18n import L

# A box made of spaces: rich has no "no border", and `box=None` is refused by
# Panel. This keeps the padding and the layout while drawing nothing.
SPACE = box.Box("\n".join(["    "] * 8))

# slot name -> {variant name: value}
#
# Built-in entries carry their own name as the value: the behaviour lives with
# the widget that knows how to draw it (components.py switches on it). A plugin
# registers a *callable* (banner, spinner) or a *class* (stream) under a new
# name, and the caller uses that instead — that is how a skin becomes more than
# a list of presets.
_VARIANTS: dict[str, dict[str, object]] = {
    "frame": {
        "rounded": {"box": box.ROUNDED},
        "heavy": {"box": box.HEAVY},
        "square": {"box": box.SQUARE},
        "ascii": {"box": box.ASCII},
        "minimal": {"box": box.MINIMAL},
        "none": {"box": SPACE},
    },
    "banner": {"shimmer": "shimmer", "static": "static", "none": "none"},
    "spinner": {"honey": "honey", "dots": "dots", "none": "none"},
    "stream": {"default": "default"},
}

# What the interface uses until the user (or a plugin) changes it.
_DEFAULTS = {"frame": "rounded", "banner": "shimmer", "spinner": "honey", "stream": "default"}

_active = dict(_DEFAULTS)

# slot -> (current value, whether it came from the user rather than a plugin)
_sources: dict[str, str] = {}


def variants(slot: str) -> list[str]:
    """Names a user can pick for this slot — built-ins and plugin registrations."""
    return list(_VARIANTS.get(slot, {}))


def register(slot: str, name: str, value=object()) -> None:
    """Offer a new variant for a slot — this is what plugins call."""
    _VARIANTS.setdefault(slot, {})[name] = value


def choose(slot: str, name: str, source: str = "user") -> bool:
    """Choose a variant. False when that name is not registered."""
    if name not in _VARIANTS.get(slot, {}):
        return False
    _active[slot] = name
    _sources[slot] = source
    return True


def get(slot: str) -> str:
    return _active.get(slot, _DEFAULTS.get(slot, ""))


def value(slot: str, default=None):
    """The registered object behind the active name.

    A dict for `frame`, a string for the built-in variants of `banner` /
    `spinner` / `stream`, and whatever a plugin registered for its own names —
    callers check with `callable()` or `isinstance(..., type)`.
    """
    return _VARIANTS.get(slot, {}).get(_active.get(slot, ""), default)


def apply(config: dict | None) -> None:
    """Take the `ui` section of beeagent.json, ignoring names we do not know."""
    for slot, name in (config or {}).items():
        if isinstance(name, str):
            choose(slot, name, source="config")


def as_dict() -> dict:
    return dict(_active)


def reset() -> None:
    _active.update(_DEFAULTS)
    _sources.clear()


def frame_kwargs(border: str = "") -> dict:
    """Arguments for `rich.table.Table(...)` that draw the current frame.

    A panel asks for its border here, so "no frames" is one setting rather than
    a change to every table in the interface. `border` is what that panel would
    have used; the "none" frame drops it along with the box.
    """
    choice = _VARIANTS["frame"].get(_active.get("frame", "rounded")) or {}
    kwargs = {"box": choice.get("box", box.ROUNDED)}
    if "border_style" in choice:
        kwargs["border_style"] = choice["border_style"]
    elif border:
        kwargs["border_style"] = border
    return kwargs


def framed() -> bool:
    """Whether panels should be boxed at all — used by code that indents instead."""
    return _active.get("frame", "rounded") != "none"


def describe() -> str:
    """One line for the user: what is switched on and who said so."""
    parts = [L(f"{slot}={name}", f"{slot}={name}") for slot, name in sorted(_active.items())]
    return "  ".join(parts)
