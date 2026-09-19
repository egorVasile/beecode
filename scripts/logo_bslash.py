"""BeeCode marks: the letter B plus a second sign, in the logo's own shades.

Same 5x5 pixel font and honey->leaf ramp as the startup banner, so any of these
can replace the wordmark without looking like a different product.

    python scripts/logo_bslash.py              all six marks, labelled
    python scripts/logo_bslash.py --pick 1     one mark, big
    python scripts/logo_bslash.py --animate    sweep the colour wave across it
"""
from __future__ import annotations

import argparse
import math
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from rich.align import Align                       # noqa: E402
from rich.console import Group                     # noqa: E402
from rich.panel import Panel                       # noqa: E402
from rich.table import Table                       # noqa: E402
from rich.text import Text                         # noqa: E402
from rich import box                               # noqa: E402

from beeagent.ui.components import (               # noqa: E402
    BORDER, GRADIENT, HONEY, PIXEL_FONT, _shimmer_color, console,
)

# New 5x5 cells. The font only knows BEECODE, so the second sign of each mark
# is defined here, drawn with the same block strokes.
MARK_FONT = dict(PIXEL_FONT)
MARK_FONT.update({
    "/":  ["    █", "   █ ", "  █  ", " █   ", "█    "],      # leans against the B
    ">":  ["    █", "   █ ", "  █  ", "   █ ", "    █"],      # prompt arrow, pointing right
    "_":  ["     ", "     ", "     ", "     ", "█████"],      # cursor
    ")":  ["  ██ ", " █  █", "█    █", " █  █", "  ██ "],      # wing, curved
    "(":  [" ██  ", "█  █ ", "█    █", "█  █ ", " ██  "],      # wing, mirrored
    ":":  ["     ", " ██  ", " ██  ", " ██  ", "     "],      # honey stripe
    "~":  ["     ", " █ █ ", "█ █ █", " █ █ ", "     "],      # flight path
    "*":  ["     ", "  █  ", " █████", "  █  ", "     "],      # pollen
})
# name, glyph string, why it reads as BeeCode
MARKS = [
    ("B/", "B/", "the letter and a slash — the shortest answer to 'who'"),
    ("B>_", "B>_", "a prompt waiting for the next command"),
    ("wings", ")B(", "wings on both sides of the letter"),
    ("stripes", "B::", "the striped abdomen of a bee"),
    ("flight", "B~", "the path a bee took to get here"),
    ("pollen", "B*", "a grain of pollen — the smallest possible mark"),
]


def rows_for(text: str, xscale: int = 3) -> list[str]:
    rows = []
    for r in range(5):
        cells = [MARK_FONT.get(ch, MARK_FONT[" "])[r] for ch in text]
        rows.append(" ".join("".join(c * xscale) for c in cells))
    return rows


def mark(rows, phase: float | None = None) -> Text:
    """Row-shaded at rest, or under a colour wave when `phase` is given."""
    width = max(len(r) for r in rows) or 1
    out = Text()
    for i, row in enumerate(rows):
        for j, char in enumerate(row):
            if char == " ":
                out.append(" ")
                continue
            if phase is None:
                color = GRADIENT[i % len(GRADIENT)]
            else:
                wave = 0.5 + 0.5 * math.cos(2 * math.pi * (j / width * 1.6 - phase))
                color = _shimmer_color(wave)
            out.append(char, style=f"bold {color}")
        if i != len(rows) - 1:
            out.append("\n")
    return out


def gallery(xscale: int) -> Group:
    parts = [Text("BeeCode marks — same font, same ramp as the banner",
                  style=f"bold {HONEY}"), Text()]
    for index, (name, glyphs, why) in enumerate(MARKS, 1):
        parts.append(Text.assemble(
            f"  {index}. ", Text(why, style="dim"),
            Text(f"   --pick {index}", style="bold " + HONEY)
        ))
        parts.append(Align.left(mark(rows_for(glyphs, xscale))))
        parts.append(Text())
    return Group(*parts)


def big(index: int, xscale: int, phase: float | None) -> Panel:
    name, glyphs, why = MARKS[index]
    return Panel(
        Align.center(mark(rows_for(glyphs, xscale), phase)),
        title=f"[bold {HONEY}]{name}[/] · {why}",
        title_align="left",
        border_style=BORDER,
        box=box.ROUNDED,
        padding=(1, 2),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--pick", type=int, default=0, help=f"1-{len(MARKS)}: show one mark alone")
    parser.add_argument("--animate", action="store_true", help="sweep the colour wave once")
    parser.add_argument("--scale", type=int, default=3, help="pixel width, 1-6")
    parser.add_argument("--no-hold", action="store_true", help="exit without waiting")
    args = parser.parse_args()
    scale = max(1, min(6, args.scale))

    if args.pick:
        index = min(max(1, args.pick), len(MARKS)) - 1
        if args.animate:
            for step in range(26):
                console.clear()
                console.print(big(index, scale, step / 26))
                time.sleep(0.04)
        console.clear()
        console.print(big(index, scale, None))
    else:
        console.clear()
        console.print(gallery(scale))
        console.print(Text("  try:  python scripts/logo_bslash.py --pick 1 --animate --scale 4",
                           style="dim"))
    if not args.no_hold:
        try:
            input("  press Enter to close ")
        except EOFError:
            pass


if __name__ == "__main__":
    main()
