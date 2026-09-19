"""Render real BeeCode output into PNG screenshots for the README.

Nothing here is mocked up: every frame comes from calling the actual UI code
(banner, streaming callbacks, tool renderers, tables, the scroll viewer) with a
truecolour rich Console, and the ANSI it emits is painted onto a grid with
Consolas plus Segoe UI Emoji. The prompt line is rendered from the REPL's own
PROMPT constant.

    python scripts/make_screenshots.py
"""
from __future__ import annotations

import io
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "screenshots"
FONT_PATH = r"C:\Windows\Fonts\consola.ttf"
BOLD_PATH = r"C:\Windows\Fonts\consolab.ttf"
EMOJI_PATH = r"C:\Windows\Fonts\seguiemj.ttf"
FONT_SIZE = 18
BG = (11, 22, 14)          # deep hive green, like the terminal in the screenshots
DEFAULT_FG = (226, 238, 224)

SGR = re.compile(r"\x1b\[([0-9;:?]*)m")
# Sequences the painter ignores. The letter class deliberately stops short of
# `m`: that is what lets the SGR colour codes above survive to be parsed.
OTHER = re.compile(
    r"\x1b[\]>][^\x07\x1b]*(?:\x07|\x1b\\)"
    r"|\x1b\[[0-9;:?]*(?:[A-Z]|[a-ln-z])"
    r"|\x1b[()][0-9A-B]"
)

XTERM = [i * 255 // 5 for i in range(6)]


def _palette256(n: int) -> tuple:
    if n < 16:
        base = [(0, 0, 0), (170, 0, 0), (0, 170, 0), (170, 85, 0), (0, 0, 170),
                (170, 0, 170), (0, 170, 170), (170, 170, 170), (85, 85, 85),
                (255, 85, 85), (85, 255, 85), (255, 255, 85), (85, 85, 255),
                (255, 85, 255), (85, 255, 255), (255, 255, 255)]
        return base[n]
    if n < 232:
        r, g, b = (n - 16) // 36, (n - 16) // 6 % 6, (n - 16) % 6
        return tuple(XTERM[v] for v in (r, g, b))
    v = 8 + (n - 232) * 10
    return (v, v, v)


def ansi_grid(text: str) -> list[list[tuple[str, tuple, bool]]]:
    """Turn an ANSI stream into rows of (character, colour, bold)."""
    text = OTHER.sub("", text.replace("\r\n", "\n").replace("\r", ""))
    rows: list[list[tuple]] = [[]]
    fg, bold = DEFAULT_FG, False
    index = 0
    while index < len(text):
        match = SGR.match(text, index)
        if match:
            params = [p for p in match.group(1).split(";") if p != ""] or ["0"]
            i = 0
            while i < len(params):
                code = int(params[i])
                if code == 0:
                    fg, bold = DEFAULT_FG, False
                elif code == 1:
                    bold = True
                elif code == 39:
                    fg = DEFAULT_FG
                elif code == 38 and i + 2 < len(params):
                    if params[i + 1] == "2":
                        fg = tuple(int(v) for v in params[i + 2:i + 5])
                        i += 4
                    elif params[i + 1] == "5":
                        fg = _palette256(int(params[i + 2]))
                        i += 2
                i += 1
            index = match.end()
            continue
        char = text[index]
        if char == "\n":
            rows.append([])
        elif char >= " " or char == "\t":
            rows[-1].append((char, fg, bold))
        index += 1
    return rows


def _is_emoji(char: str) -> bool:
    cp = ord(char)
    return cp >= 0x1F000 or (0x2300 <= cp <= 0x2BFF and not 0x2500 <= cp <= 0x25FF)


def capture(render, width: int = 96) -> str:
    """ANSI produced by the real UI for `render` (a callable or a renderable).

    The UI modules print through their module-level `console`, so that name is
    swapped for a truecolour console writing into a buffer. Redirecting
    sys.stdout instead would strip every colour: rich would see a non-terminal.
    """
    import beeagent.ui.components as comp
    import beeagent.ui.repl as repl
    from rich.console import Console

    buffer = io.StringIO()
    console = Console(file=buffer, width=width, color_system="truecolor", force_terminal=True)
    originals = (comp.console, repl.console)
    comp.console = repl.console = console
    try:
        if callable(render):
            render(console)
        else:
            console.print(render)
    finally:
        comp.console, repl.console = originals
    return buffer.getvalue()


def paint(rows, path: Path) -> None:
    mono = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    bold_font = ImageFont.truetype(BOLD_PATH, FONT_SIZE)
    emoji = ImageFont.truetype(EMOJI_PATH, FONT_SIZE)
    cell = mono.getlength("M")
    line_h = int(FONT_SIZE * 1.55)
    width = int(max((len(r) for r in rows), default=10) * cell) + 48
    height = len(rows) * line_h + 40
    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)
    drawn = {}

    def font_for(char):
        """Consolas for text and box art, the colour emoji font for pictographs."""
        return emoji if _is_emoji(char) else mono

    for y, row in enumerate(rows):
        x = 24
        for char, color, is_bold in row:
            face = font_for(char)
            if face is emoji:
                draw.text((x, 20 + y * line_h), char, font=emoji, embedded_color=True)
                x += cell * 2
            else:
                draw.text((x, 20 + y * line_h), char, fill=color,
                          font=bold_font if (is_bold and face is mono) else face)
                x += cell
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    print(f"  {path.relative_to(ROOT)}  {width}x{height}")


def main() -> None:
    from rich.text import Text
    from beeagent.ui import components as comp
    from beeagent.ui.repl import PROMPT, handle_callback
    from beeagent.ui.commands import COMMANDS, ReplContext, dispatch
    from beeagent.config.schema import BeeConfig
    from beeagent.core.session import Session
    
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prompt_line = re.sub(r"<[^>]+>", "", PROMPT.value).replace("&gt;", ">")
    # A real Agent: /tools must show the live registry, plugins included.
    from beeagent.core.agent import Agent
    ctx = ReplContext(agent=Agent(config=BeeConfig()), config=BeeConfig(), session=Session())

    def logo(console):
        """The real startup banner, exactly as print_banner draws it at rest."""
        comp.print_banner(animate=False)

    def session(console):
        console.print(Text(f"{prompt_line}create a test file and run it", style="bold #8fbf6f"))
        handle_callback("status", {})
        handle_callback("reasoning_delta", {"text": "The user wants a test file.\n"})
        handle_callback("stream_delta", {"text": "Creating tests/test_demo.py first.\n"})
        handle_callback("tool_start", {"tool": "write",
                                       "args": {"path": "tests/test_demo.py",
                                                "content": "def test_demo():\n    assert 2 + 2 == 4\n"}})
        handle_callback("tool_end", {"tool": "write",
                                     "args": {"path": "tests/test_demo.py", "content": ""},
                                     "output": "", "error": False})
        handle_callback("tool_start", {"tool": "bash", "args": {"command": "pytest tests/test_demo.py -q"}})
        handle_callback("tool_end", {"tool": "bash", "args": {"command": "pytest"},
                                     "output": "1 passed in 0.12s", "error": False})
        handle_callback("done", {})

    frames = {
        "01-banner.png": logo,
        "02-session.png": session,
        "03-tools.png": lambda c: c.print(dispatch(ctx, "/tools").output),
        "04-commands.png": lambda c: c.print(comp.commands_table(COMMANDS)),
    }
    for name, render in frames.items():
        paint(ansi_grid(capture(render, width=112 if name.startswith("01") else 96)), OUT_DIR / name)


if __name__ == "__main__":
    main()
