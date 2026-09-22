"""Remove the terminal control sequences that a model, a plugin or a file can use
to act on the user's machine through our own window.

This is not cosmetic. rich strips BEL, BS, VT, FF and CR from text, and leaves
`ESC` alone — so a reply containing `ESC ] 52 ; c ; <base64> BEL` reached the
terminal byte for byte through `console.print(Text(...))`. That is a clipboard
write: the model puts a command in the clipboard, the user presses Ctrl+V at a
shell prompt, and nothing on screen claimed responsibility. The same channel sets
the window title, clears or repaints the screen to hide what was really run, and
with OSC 8 draws a link whose visible text and target are both model-chosen.

The bytes do not have to come from the model. A hostile repository puts them in a
file, the user asks BeeCode to read it, and the same renderer prints them.

Order matters: whole sequences are removed before single characters, otherwise
`ESC ESC ] 0 ; …` reassembles into a live OSC after the first pass.
"""
from __future__ import annotations

import re

# CSI: ESC [ , parameter bytes, intermediate bytes, one final byte in @-~. This is
# the family that clears the screen, moves the cursor and rewrites lines.
_CSI = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]")
# ESC followed by a introducer, up to ST (ESC \) or BEL. OSC, DCS, SOS, PM, APC
# and the private-use ones all fit this shape — OSC 52 (clipboard) and OSC 8
# (hyperlink) are the two that matter most.
_INTRODUCED = re.compile(
    "\x1b"                                   # ESC
    r"[\]P^_X>=]"                             # what kind of sequence
    r"(?:.|\n)*?(?:\x1b\\|\x07|\x1b\x5c)",    # terminated by ST or BEL
    re.DOTALL,
)
# An unterminated sequence at the end of the text still must not reach the screen.
_DANGLING = re.compile(r"\x1b(?:\[|\]|P|_|\^)[^\x07\x1b]*$")
_LONE_ESC = re.compile("\x1b[@-Z\\\\-_]?")
_C1 = re.compile("[\x80-\x9f]")
# Trojan-source family: invisible characters that let a displayed path differ from
# the path that is written, and zero-widths that hide what a string really says.
_BIDI_AND_ZERO_WIDTH = re.compile("[\u202a-\u202e\u2066-\u2069\u200b-\u200d\ufeff\u2064]")
_OTHER_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1a\x1c-\x1f]")


def strip_terminal(text: str) -> str:
    """Text that is safe to hand to a terminal emulator, with its meaning intact."""
    if not isinstance(text, str) or "\x1b" not in text and not _needs_work(text):
        return text if isinstance(text, str) else ""
    cleaned = _CSI.sub("", text)
    cleaned = _INTRODUCED.sub("", cleaned)
    cleaned = _DANGLING.sub("", cleaned)
    cleaned = _LONE_ESC.sub("", cleaned)
    cleaned = _C1.sub("", cleaned)
    cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n")
    cleaned = _OTHER_C0.sub("", cleaned)
    return _BIDI_AND_ZERO_WIDTH.sub("", cleaned)


def _needs_work(text: str) -> bool:
    return any(char in text for char in ("\r", "\u202a", "\u200b", "\ufeff")) or \
        bool(_C1.search(text))


def has_terminal_risk(text: str) -> bool:
    """Whether stripping changed anything — used by tests and by /doctor."""
    return strip_terminal(text) != text
