"""A chunky pixel bee that flaps its wings and waves a paw.

Frames are plain ASCII so they are encoding-safe; colour is applied per
character via rich styles when rendering. Palette:
    Y = body (yellow)   K = stripes / legs (brown)
    W = wings (pale blue)   E = eye (white)   P = waving paw (orange)
"""
from __future__ import annotations

from rich.text import Text

BEE_COLORS = {
    "Y": "bold #ffcc00",
    "K": "bold #7a5200",
    "W": "bold #bfe9ff",
    "E": "bold #ffffff",
    "P": "bold #ff8a00",
}

BEE_FRAMES = [
    [
        "   WW   WW   ",
        "  WWW   WWW  ",
        "  EYYYYYYYY  ",
        "  YYKYYYKYY  ",
        "  YYYYYYYYY  ",
        "   YYYYYYY   ",
        "  PP  K   K  ",
        "      K   K  ",
    ],
    [
        "             ",
        "   WW   WW   ",
        "  EYYYYYYYY  ",
        "  YYKYYYKYY  ",
        "  YYYYYYYYY  ",
        "   YYYYYYY   ",
        "  P   K   K  ",
        "  P   K   K  ",
    ],
    [
        "   WW   WW   ",
        "  WWW   WWW  ",
        "  EYYYYYYYY  ",
        "  YYKYYYKYY  ",
        "  YYYYYYYYY  ",
        "   YYYYYYY   ",
        "      K   K  ",
        "  PP  K   K  ",
    ],
    [
        "             ",
        "   WW   WW   ",
        "  EYYYYYYYY  ",
        "  YYKYYYKYY  ",
        "  YYYYYYYYY  ",
        "   YYYYYYY   ",
        "  P   K   K  ",
        "  P   K   K  ",
    ],
]

BEE_FRAME_COUNT = len(BEE_FRAMES)


def render_bee(frame_index: int = 0) -> Text:
    """Render one animation frame as a coloured rich Text object."""
    rows = BEE_FRAMES[frame_index % BEE_FRAME_COUNT]
    text = Text()
    last = len(rows) - 1
    for i, row in enumerate(rows):
        for ch in row:
            if ch == " ":
                text.append(" ")
            else:
                text.append(ch, style=BEE_COLORS.get(ch, "white"))
        if i != last:
            text.append("\n")
    return text
