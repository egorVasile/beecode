"""A phone keeps its width: on a narrow terminal the chrome steps out of the chat.

BeeCode also runs in Termux, where the screen is 40–60 columns. There the opening
is a 76-column pixel logo that wraps into a pile of blocks, every panel spends a
column on each border, and the full-screen TUI docks a 40-column command sidebar —
more than half of the terminal — next to the answer. `compact` looks at the
terminal once, when it loads, and on a narrow one gives the space back to the
chat: a one-line opening and no frames. On a wide one it changes nothing, and it
never touches a colour — this is layout, not a theme.

The sidebar itself is out of reach from here: its width is written into the app's
own CSS rather than a skin slot, so on the full-screen TUI this plugin can only
clear the chrome around the chat.
"""
from __future__ import annotations

import shutil

from beeagent.i18n import L

NARROW = 100


def columns() -> int:
    """How wide the terminal is right now — `$COLUMNS` included, Termux sets it."""
    return int(shutil.get_terminal_size().columns or 0)


def setup(api) -> None:
    api.setting("narrow_below", NARROW, "treat a terminal narrower than this as a phone")
    if columns() >= int(api.get("narrow_below") or NARROW):
        return

    from beeagent.ui import skin

    api.skin("banner", "one-line", _one_line)
    api.set_skin("banner", "one-line")
    api.set_skin("frame", "none")


def _one_line() -> None:
    """The opening the `banner` slot asks for: one line, no wipe, no wrap."""
    from beeagent.ui.components import console

    console.print(L("🐝 BeeCode — free AI coding agent",
                    "🐝 BeeCode — бесплатный ИИ-агент для кода"))
