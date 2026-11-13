# MIT License — this file is additionally available under the MIT licence, in
# parallel with the repository's GPL-3.0-or-later, so the BeeCode key pool may
# list it in its marketplace index (which only carries MIT-compatible entries):
#
# Copyright (c) 2026 egorVasile
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

"""`skin-claude` — the Claude Code look, rebuilt as surfaces.

A coral pixel wordmark, a crab that greets you, per-role coral borders and the
`●` tool lines from the screenshots — and nothing that moves on its own. Every
hook below is a pure function of its arguments (the spinner reads the clock it
is handed, never the wall), so a turn can be replayed in a test and draws the
same pixels twice.

What it demonstrates about the engine:

*   **nine surfaces, zero animation**: `banner`, `frame`, `status`, `spinner`,
    `thinking`, `welcome`, `error`, `tool_start`, `tool_end` — the whole chrome
    around the answer, while the answer itself stays the model's (no `answer`
    claim, so nothing the model wrote is ever restyled);
*   **markup, not paint calls**: line surfaces answer with Rich markup the host
    parses, which is why a stray `[/]` in a tool name degrades to literal text
    instead of taking the line down;
*   **narrow terminals**: the wordmark falls back to one plain line when the box
    it was given is too small, because a logo that wraps is not a logo.

One import — `beeagent.core.skins`, the exact spelling the gate allows (like
the prism pack) — used only by the `setup()` fallback when the host's own
verbs are missing. Letters, colours and the crab live below as data.
"""
import beeagent.core.skins as skinshost

NAME = "claude"
PACK = "skin-claude"
DESCRIPTION = "Claude Code look: coral pixel logo, crab mascot, ● tool lines"

# The clawd coral, measured off the client itself: rgb(215, 119, 87), with two
# darker steps for depth.
CORAL = "#D77757"
CORAL_DEEP = "#C96A4D"
CORAL_DARK = "#A9583F"
ERROR_RED = "#E05555"

# One pixel letter, five rows of it. █ is what the shipped banner paints with,
# so the wordmark sits in the same family instead of inventing another one.
FONT = {
    # Every row is exactly five cells: the wordmark code pads nothing and
    # trims nothing, so equal rows in means equal lines out.
    "A": ("  █  ",
          " █ █ ",
          "█████",
          "█   █",
          "█   █"),
    "C": (" ███ ",
          " █   ",
          " █   ",
          " █   ",
          " ███ "),
    "D": ("████ ",
          "█  █ ",
          "█  █ ",
          "█  █ ",
          "████ "),
    "E": ("█████",
          "█    ",
          "████ ",
          "█    ",
          "█████"),
    "L": ("█    ",
          "█    ",
          "█    ",
          "█    ",
          "█████"),
    "O": (" ███ ",
          "█   █",
          "█   █",
          "█   █",
          " ███ "),
    "U": ("█  █ ",
          "█  █ ",
          "█  █ ",
          "█  █ ",
          "█████"),
    " ": ("     ",
          "     ",
          "     ",
          "     ",
          "     "),
}

# A letter's shade walks the ramp, so the word has depth instead of one flat
# fill. Three stops are enough: more would read as a rainbow, not a logo.
SHADES = (CORAL, CORAL_DEEP, CORAL_DARK)

# The clawd mascot, drawn the way the client draws it: sparkles, a three-row
# crest, feet. Block quarters, like the shipped banner's own blocks.
CRAB = ("*       *",
        " ▐▛███▜▌ ",
        " ▝▜█████▛▘",
        "   ▘▘ ▝▝  ")

LANG = "en"


def shade(index: int) -> str:
    return SHADES[index % len(SHADES)]


def wordmark(rows: int):
    """The CLAUDE / CODE rows as (text, colour) runs, clipped to `rows`.

    Every glyph is padded to the same width, so every line comes out the same
    length: a ragged right edge is what makes pixel letters unreadable.
    """
    words = ("CLAUDE", "CODE")
    runs = []
    for line in range(5):
        parts = []
        for word in words:
            for pos, letter in enumerate(word):
                glyph = FONT.get(letter, FONT[" "])[line].ljust(5)
                parts.append((glyph + " ", shade(pos)))
            parts.append((" ", CORAL))
        # Invisible tail costs nothing on screen but breaks every width the
        # host measures, so it goes before the markup is built — and every
        # line is then padded back to the same length, so a ragged edge never
        # reads as a broken letter.
        while parts and not parts[-1][0].strip():
            parts.pop()
        if parts:
            text, colour = parts[-1]
            parts[-1] = (text.rstrip(), colour)
            if not parts[-1][0]:
                parts.pop()
        runs.append(parts)
    wide = 0
    for parts in runs:
        wide = max(wide, sum(len(text) for text, colour in parts))
    for parts in runs:
        short = wide - sum(len(text) for text, colour in parts)
        if short > 0 and parts:
            text, colour = parts[-1]
            parts[-1] = (text + " " * short, colour)
    return runs[:max(1, rows)]


def paint(runs) -> str:
    """(text, colour) runs as one markup string the host can print."""
    out = []
    for parts in runs:
        line = "".join("[%s]%s[/]" % (colour, text) for text, colour in parts)
        out.append(line.rstrip())
    return "\n".join(out)


SURFACES = ("banner", "frame", "status", "spinner", "thinking", "welcome",
            "error", "tool_start", "tool_end")


def on_init(ctx=None):
    """Remember the language once; everything else is stateless."""
    global LANG
    try:
        language = ctx.language() if ctx is not None else "en"
    except Exception:
        language = "en"
    LANG = language if language in ("en", "ru") else "en"
    return None


def on_banner(seconds, width=0, rows=0, default=None):
    """The coral wordmark, or one plain line when the box is too small."""
    try:
        wide = int(width or 0)
    except Exception:
        wide = 0
    if wide and wide < 30:
        return "CLAUDE CODE"
    return paint(wordmark(rows or 5))


def on_frame_color(role="", seconds=0.0):
    """A coral border per panel role; errors stay in the red family."""
    word = str(role or "").strip().lower()
    if word == "error":
        return "bold " + ERROR_RED
    shades = {"answer": CORAL, "tool": CORAL_DEEP, "output": CORAL_DARK,
              "thinking": CORAL_DEEP, "prompt": CORAL, "picker": CORAL_DARK}
    return "bold " + shades.get(word, CORAL)


def on_status(default):
    return "✳ " + str(default)


def on_spinner(default, clock=0.0):
    """A two-frame pulse read off the handed clock — deterministic in tests."""
    try:
        dot = "●" if int(float(clock) * 2) % 2 == 0 else "○"
    except Exception:
        dot = "●"
    return dot + " " + str(default)


def on_thinking(line):
    return "│ " + str(line)


def on_welcome(default):
    """The crab greets you, then the host's own welcome line."""
    hello = "Welcome back!" if LANG != "ru" else "С возвращением!"
    crab = "\n".join("[%s]%s[/]" % (CORAL_DEEP, row) for row in CRAB)
    return hello + "\n" + crab + "\n" + str(default)


def on_error(default):
    return "✗ " + str(default)


def on_tool_start(summary, tool):
    return "● " + str(summary)


def on_tool_end(summary, tool, error=False):
    mark = "✗" if error else "●"
    return mark + " " + str(summary)


def hooks() -> dict:
    """This pack's lifecycle, by name — what `setup` hands to the host."""
    return {"SURFACES": SURFACES, "NAME": NAME,
            "on_init": on_init, "on_banner": on_banner,
            "on_frame_color": on_frame_color, "on_status": on_status,
            "on_spinner": on_spinner, "on_thinking": on_thinking,
            "on_welcome": on_welcome, "on_error": on_error,
            "on_tool_start": on_tool_start, "on_tool_end": on_tool_end}


# ------------------------------------------------------------------ registration ---

def setup(api) -> None:
    """Hand the skin to the host through whichever verb it answers to."""
    given = hooks()
    try:
        api.skin_hooks(NAME, given, DESCRIPTION)
        return
    except Exception:
        pass
    try:
        api.skin_hooks(NAME, given)
        return
    except Exception:
        pass
    try:
        skinshost.register(NAME, given, description=DESCRIPTION, pack=PACK)
    except Exception:
        pass
