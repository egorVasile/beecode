"""The render engine a skin paints on: a cell grid, a diff, a clocked frame loop.

Until now a "skin" in BeeCode was a colour dict (``ui/skin.py``) and everything
that actually draws lives in the widget layer. This module is the other half: a
real frame cycle a skin is handed and paints onto, without the skin ever touching
an escape sequence.

Three pieces, in the order a skin meets them:

* `Grid` — an offscreen buffer of cells. Nothing reaches the terminal until a whole
  frame exists, so a half-painted frame can never be seen.
* `Painter` — the surface a skin calls. Exactly six methods (`draw_text`,
  `draw_box`, `clear_region`, `get_terminal_size`, `color_support`, `color`)
  because two other layers code against them, and they are the contract.
* `FrameLoop` — the clock: ask the skin to paint, diff the two grids, write only
  what changed, wait for the next beat.

Deliberate limits, and why:

* **Standard library only.** No ``blessed``, no curses (absent on stock Windows),
  no new dependency, no new process. The one non-obvious import is
  ``utils/sanitize.strip_terminal``, and that is a security import rather than a
  convenience: a skin's text is model-adjacent, and an OSC 52 inside a label would
  otherwise be a clipboard write delivered from inside our own window.
* **Runs everywhere or nowhere.** Windows Terminal, plain conhost (whose cp1251
  code page cannot even encode our box glyphs, hence UTF-8 at the byte layer),
  Termux, and a dumb pipe. A pipe gets no escape codes at all: `FrameLoop.run`
  says so once and draws nothing, because colour codes inside
  ``beecode | grep something`` break the *other* program, not us.
* **A skin cannot kill the UI.** Every public call clips bad coordinates, falls
  back on bad colours, and swallows its own exceptions into `frame_warnings` for
  the host to surface. A skin that raises loses its frame, not the terminal: the
  cursor and the primary screen come back on every exit path, including the one
  that leaves through an exception.

Not built, on purpose: mouse tracking, the kitty graphics protocol, synchronized
output (``?2026``), and 24-bit-style styling beyond SGR. Each is a flag a host can
add later; none is needed to paint a cell grid, and two of them silently do
nothing on half the targets this has to run on.

---------------------------------------------------------------------------
Width: the one assumption in this file
---------------------------------------------------------------------------

``tools/base.py`` had to learn that a *character* count is not a *byte* count —
`write` once announced "Written 13 bytes" for a 25-byte Cyrillic file. This
renderer is the mirror image of that mistake, so the rule is stated once and
obeyed everywhere: **every coordinate, count and return value here is a CELL (a
terminal column)** — never a byte, never a Python character.

The classification is deliberately dumb, because nothing smarter is reliable
across the four targets above:

* a combining mark rides along in the cell of the base character (0 new cells);
* `unicodedata.east_asian_width` of ``"W"``/``"F"`` -> 2 cells;
* everything else -> 1 cell.

Assumed: the terminal sits in its default state — ambiguous width *off* (so
Cyrillic and box-drawing are 1 cell, which is what Windows Terminal, conhost and
Termux all do) and emoji presentation *not* forced double-wide (``🙂`` counts as 1
here even though many emulators draw it as 2). A wrong guess cannot crash anything:
it can only shift a glyph inside the row that holds it, every row begins with an
absolute cursor move, so drift is corrected by the next row of the same frame and
the grid model stays intact for the next diff. A miscalculation costs one
mis-placed glyph for one frame; it never tears the screen and never raises.
"""
from __future__ import annotations

import math
import os
import shutil
import sys
import threading
import time
import unicodedata

from beeagent.i18n import L
from beeagent.utils.sanitize import strip_terminal

# A terminal that reports 0 columns — or 100000 because something exported a wild
# $COLUMNS — must not make us allocate a monster or divide by zero.
DEFAULT_SIZE = (80, 24)
MAX_DIM = 1024

# Tabs lay out to this column, like every terminal's own TAB.
TAB_STOP = 8

# 60fps of text is not a feature, it is flicker. The loop clamps into this range.
DEFAULT_FPS = 12
MIN_FPS = 1.0
MAX_FPS = 30.0

# Warnings are a bounded set of *distinct* messages: a skin that mis-positions in a
# loop must not grow memory without limit.
MAX_WARNINGS = 64
MAX_CELL_CACHE = 4096
MAX_COLOR_CACHE = 8192

# ---------------------------------------------------------------------------
# Colour: detection, palettes, degradation
# ---------------------------------------------------------------------------

TRUECOLOR = "truecolor"
DEPTH_256 = "256"
DEPTH_16 = "16"
DEPTHS = (TRUECOLOR, DEPTH_256, DEPTH_16)

# Reference RGB for the 16 colours. They only answer "which of the 16 is nearest to
# this RGB" — when we actually ask for one of the 16 we emit 30-37/90-97, so the
# user's own theme wins over these numbers.
ANSI16_RGB = (
    (0, 0, 0), (128, 0, 0), (0, 128, 0), (128, 128, 0),
    (0, 0, 128), (128, 0, 128), (0, 128, 128), (192, 192, 192),
    (128, 128, 128), (255, 0, 0), (0, 255, 0), (255, 255, 0),
    (0, 0, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255),
)


def _xterm256_rgb():
    """The 216-step cube plus the 24 grey steps, after the base 16."""
    levels = (0, 95, 135, 175, 215, 255)
    cube = [(levels[r], levels[g], levels[b])
            for r in range(6) for g in range(6) for b in range(6)]
    grey = [(8 + 10 * i,) * 3 for i in range(24)]
    return ANSI16_RGB + tuple(cube) + tuple(grey)


XTERM256_RGB = _xterm256_rgb()

# Names a skin may use. The basic 16 resolve to an ANSI index, so `red` stays the
# *terminal's* red at every depth; the BeeCode names below are real RGB and degrade
# like any other truecolour value.
PALETTE = {
    "black": 0, "red": 1, "green": 2, "yellow": 3, "brown": 3, "olive": 3,
    "blue": 4, "magenta": 5, "purple": 5, "cyan": 6, "teal": 6,
    "white": 7, "grey": 7, "gray": 7, "silver": 8,
    "bright_black": 8, "dark_grey": 8, "dark_gray": 8,
    "bright_red": 9, "light_red": 9, "bright_green": 10, "light_green": 10,
    "bright_yellow": 11, "light_yellow": 11, "bright_blue": 12, "light_blue": 12,
    "bright_magenta": 13, "light_magenta": 13, "bright_cyan": 14, "light_cyan": 14,
    "bright_white": 15,
}

# The logo colours `ui/components.py` draws with, so a skin can say "honey" instead
# of copying a hex literal out of the widget layer.
NAMED_RGB = {
    "honey": (255, 204, 0),
    "leaf": (124, 179, 66),
    "dark_leaf": (67, 160, 71),
    "amber": (246, 168, 33),
    "hive": (138, 109, 0),
    "flash": (255, 253, 231),
    "cream": (255, 249, 196),
    "lime": (192, 202, 51),
    "ink": (33, 33, 33),
    "paper": (250, 250, 245),
}

_STYLE_CODES = {
    "bold": "1", "bright": "1", "dim": "2", "faint": "2", "italic": "3",
    "underline": "4", "blink": "5", "reverse": "7", "inverse": "7",
    "hidden": "8", "conceal": "8", "strike": "9", "strikethrough": "9",
}
_WORDS_THAT_ARE_NOT_COLOUR = set(_STYLE_CODES) | {"normal", "reset"}

_support_cache = None
_color_cache = {}
_sgr_cache = {}
_cell_cache = {}


def detect_color_support(env=None) -> str:
    """Read COLORTERM/TERM (and Windows' own signals) and pick a colour depth."""
    env = os.environ if env is None else env
    term = (env.get("TERM") or "").lower()
    colorterm = (env.get("COLORTERM") or "").lower()
    if term == "dumb":
        # A terminal that cannot address the cursor either; we still say 16 rather
        # than invent a fourth depth the contract does not have.
        return DEPTH_16
    if colorterm in ("truecolor", "24bit", "direct"):
        return TRUECOLOR
    if colorterm in ("256color", "256"):
        return DEPTH_256
    if env.get("WT_SESSION") or "windows terminal" in (env.get("TERM_PROGRAM") or "").lower():
        # Windows Terminal does truecolor; some builds forget to say so in COLORTERM.
        return TRUECOLOR
    if "256color" in term or "256" in term or "direct" in term:
        return DEPTH_256
    if term.startswith(("xterm", "screen", "tmux", "foot", "alacritty", "kitty")) or term in (
            "ansi", "vt100", "linux"):
        # Modern emulators that only say "xterm" do 256 at a minimum, but we will
        # not guess truecolour from a bare TERM: a skin that asks for an exact RGB
        # sees the nearest cube colour. Predictable beats pretty.
        return DEPTH_256
    return DEPTH_16


def color_support(refresh: bool = False) -> str:
    """The depth this terminal speaks, detected once and then cached.

    Cached because a skin asks per draw call, and because a depth that changed
    mid-frame would make one skin look different on one screen.
    """
    global _support_cache
    if _support_cache is None or refresh:
        _support_cache = detect_color_support()
    return _support_cache


def _sq_distance(a, b) -> int:
    dr, dg, db = a[0] - b[0], a[1] - b[1], a[2] - b[2]
    return dr * dr + dg * dg + db * db


def nearest_256(rgb) -> int:
    """xterm-256 index nearest to `rgb`; ties go to the lower index, always."""
    best, best_index = None, 0
    for index, candidate in enumerate(XTERM256_RGB):
        score = _sq_distance(rgb, candidate)
        if best is None or score < best:
            best, best_index = score, index
    return best_index


def nearest_16(rgb) -> int:
    """One of the 16 ANSI colours nearest to `rgb`; ties go to the lower index."""
    best, best_index = None, 0
    for index, candidate in enumerate(ANSI16_RGB):
        score = _sq_distance(rgb, candidate)
        if best is None or score < best:
            best, best_index = score, index
    return best_index


def _parse_hex(text: str):
    body = text[1:]
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    if len(body) != 6 or any(ch not in "0123456789abcdef" for ch in body):
        return None
    return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))


def _coerce_rgb(value):
    """Any accepted colour value -> (r, g, b), or None. Never raises."""
    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text in ("default", "none", "transparent"):
            return None
        if text.startswith("#"):
            return _parse_hex(text)
        return NAMED_RGB.get(text)
    if isinstance(value, (tuple, list)) and len(value) == 3:
        try:
            rgb = tuple(int(round(float(part))) for part in value)
        except (TypeError, ValueError):
            return None
        if all(0 <= channel <= 255 for channel in rgb):
            return rgb
    return None


def parse_token(value: str):
    """`"#rrggbb"` / `"x256:n"` / `"x16:n"` -> ("hex"|"c256"|"c16", payload), else None."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if text.startswith("#"):
        rgb = _parse_hex(text)
        return ("hex", rgb) if rgb else None
    for prefix, kind, limit in (("x256:", "c256", 256), ("x16:", "c16", 16)):
        if text.startswith(prefix):
            try:
                index = int(text[len(prefix):])
            except ValueError:
                return None
            return (kind, index) if 0 <= index < limit else None
    return None


def _degrade_rgb(rgb, depth: str) -> str:
    key = (rgb, depth)
    cached = _color_cache.get(key)
    if cached is not None:
        return cached
    if depth == TRUECOLOR:
        token = "#%02x%02x%02x" % rgb
    elif depth == DEPTH_256:
        token = "x256:%d" % nearest_256(rgb)
    else:
        token = "x16:%d" % nearest_16(rgb)
    if len(_color_cache) < MAX_COLOR_CACHE:
        _color_cache[key] = token
    return token


def degrade(value, depth: str):
    """Colour value -> the canonical token for `depth`; None means "no colour".

    Deterministic: nearest neighbour on squared RGB distance with ties going to the
    lower index, so the same skin looks the same on the same terminal every time.
    The skin never sees the approximation as a failure — it asked for honey and got
    the closest thing this terminal can show.
    """
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip().lower()
        if not text or text in ("default", "none", "transparent"):
            return None
        if text in PALETTE:
            # A basic name stays a basic name at every depth: 30-37/90-97 honour
            # the user's theme, which a fixed RGB would overwrite.
            return "x16:%d" % PALETTE[text]
        if text in _WORDS_THAT_ARE_NOT_COLOUR:
            # "bold" is a style, not a colour: callers get the default, not a crash.
            return None
        token = parse_token(text)
        if token:
            kind, payload = token
            if kind == "c16":
                return "x16:%d" % payload
            if kind == "c256":
                if depth == DEPTH_16:
                    return "x16:%d" % nearest_16(XTERM256_RGB[payload])
                return "x256:%d" % payload
            return _degrade_rgb(payload, depth)
        rgb = _coerce_rgb(text)
        if rgb is None:
            return "unknown"
        return _degrade_rgb(rgb, depth)
    rgb = _coerce_rgb(value)
    if rgb is None:
        return "unknown"
    return _degrade_rgb(rgb, depth)


def token_rgb(token: str):
    """The RGB a token stands for (for blending and warnings); None for basic 16."""
    parsed = parse_token(token)
    if not parsed:
        return None
    kind, payload = parsed
    if kind == "hex":
        return payload
    return XTERM256_RGB[payload] if kind == "c256" else ANSI16_RGB[payload]


def _basic(index: int, is_foreground: bool) -> str:
    """SGR for one of the 16: 30-37/40-47 dark, 90-97/100-107 bright."""
    if index < 8:
        return str(index + (30 if is_foreground else 40))
    return str(index + (82 if is_foreground else 92))


def sgr_params(fg, bg, style, depth: str) -> str:
    """Parameters of one SGR sequence, degraded to `depth`. "" means the default."""
    parts = [_STYLE_CODES[name] for name in sorted(style) if name in _STYLE_CODES]

    def one(value, is_foreground):
        if value is None:
            return None
        resolved = value if parse_token(value) else degrade(value, depth)
        if resolved in (None, "unknown"):
            return None
        kind, payload = parse_token(resolved)
        if kind == "c16":
            # 30-37 and 40-47 for the dark eight, 90-97 and 100-107 for the bright
            # eight: basic codes, so the user's theme is what they see.
            return _basic(payload, is_foreground)
        rgb = payload if kind == "hex" else XTERM256_RGB[payload]
        if depth == TRUECOLOR and kind == "hex":
            return "%s;2;%d;%d;%d" % ("38" if is_foreground else "48",
                                      rgb[0], rgb[1], rgb[2])
        if depth != DEPTH_16:
            return "%s;5;%d" % ("38" if is_foreground else "48",
                                payload if kind == "c256" else nearest_256(rgb))
        return _basic(nearest_16(rgb), is_foreground)

    for value, is_foreground in ((fg, True), (bg, False)):
        code = one(value, is_foreground)
        if code is not None:
            parts.append(str(code))
    return ";".join(str(part) for part in parts)


def sgr(fg=None, bg=None, style=(), depth=None) -> str:
    """The whole escape for one attribute set, cached by (attributes, depth)."""
    depth = depth or color_support()
    frozen = style if isinstance(style, frozenset) else frozenset(style)
    key = (fg, bg, tuple(sorted(frozen)), depth)
    cached = _sgr_cache.get(key)
    if cached is not None:
        return cached
    params = sgr_params(fg, bg, frozen, depth)
    result = "\x1b[%sm" % params if params else ""
    if len(_sgr_cache) < MAX_COLOR_CACHE:
        _sgr_cache[key] = result
    return result


# ---------------------------------------------------------------------------
# Width maths
# ---------------------------------------------------------------------------

# Control characters `strip_terminal` leaves in on purpose; none of them is a cell,
# and letting one through would move the real cursor behind our model's back.
_NON_CELL = set("\x00\x07\x08\r\n\t")


def char_width(ch: str) -> int:
    """Cells one character occupies, under the assumption stated in the header."""
    if not ch:
        return 0
    if ch in _NON_CELL:
        return 0
    if unicodedata.combining(ch):
        return 0
    if unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Co"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def cell_width(cluster: str) -> int:
    """Width of a cell's content: the base character decides, marks ride along."""
    return char_width(cluster[0]) if cluster else 0


def text_cells(text: str) -> int:
    """Columns `text` will occupy on the grid. Cells, not bytes, not characters."""
    if not isinstance(text, str):
        return 0
    return sum(char_width(ch) for ch in text)


def clamp_size(cols, rows) -> tuple[int, int]:
    """A size we can allocate without being fooled by a nonsense $COLUMNS."""
    try:
        cols, rows = int(cols), int(rows)
    except (TypeError, ValueError):
        return DEFAULT_SIZE
    return (min(max(cols, 1), MAX_DIM), min(max(rows, 1), MAX_DIM))


def terminal_size(stream=None) -> tuple[int, int]:
    """(cols, rows) of `stream`, or a sane 80x24 when there is nothing to ask."""
    stream = stream if stream is not None else sys.stdout
    try:
        if stream.isatty():
            size = os.get_terminal_size(stream.fileno())
            if size.columns and size.lines:
                return clamp_size(size.columns, size.lines)
    except Exception:
        # Not a TTY, no fileno (a StringIO in a test), a wrapper whose isatty()
        # raises: none of these is a reason to lose the frame.
        pass
    # shutil reads COLUMNS/LINES, then the tty, then the fallback we hand it.
    size = shutil.get_terminal_size(fallback=DEFAULT_SIZE)
    return clamp_size(size.columns, size.lines)


def clamp_fps(value) -> float:
    """The frame rate the loop will actually use, inside [MIN_FPS, MAX_FPS]."""
    try:
        fps = float(value)
    except (TypeError, ValueError):
        return float(DEFAULT_FPS)
    if not math.isfinite(fps) or fps <= 0:
        return float(DEFAULT_FPS)
    return min(MAX_FPS, max(MIN_FPS, fps))


def normalize_style(style):
    """`"bold underline"` / iterable / frozenset -> (frozenset, dropped_words)."""
    if style is None:
        return frozenset(), ()
    if isinstance(style, str):
        words = style.replace(",", " ").split()
    elif isinstance(style, (frozenset, set, list, tuple)):
        words = [str(item) for item in style]
    else:
        return frozenset(), (repr(style),)
    known, dropped = set(), []
    for word in words:
        low = word.strip().lower()
        if not low:
            continue
        if low in _STYLE_CODES:
            known.add(low)
        elif low in ("normal", "reset"):
            known.clear()
        else:
            dropped.append(low)
    return frozenset(known), tuple(dropped)


# ---------------------------------------------------------------------------
# Cell and Grid
# ---------------------------------------------------------------------------

_NO_STYLE = frozenset()


class Cell:
    """One grid cell: what to show, and how to show it.

    `columns`, `attr` and `key` are worked out when the cell is made, not when the
    diff runs: a frame that touches every cell of 80x24 would otherwise rebuild a
    tuple and ask `unicodedata` per cell, which was the most expensive thing this
    renderer did. Cells are interned by `make_cell`, so the cost is paid once per
    distinct cell for the life of the process.
    """

    __slots__ = ("ch", "fg", "bg", "style", "columns", "attr", "key")

    def __init__(self, ch: str, fg=None, bg=None, style=_NO_STYLE):
        self.ch = ch
        self.fg = fg
        self.bg = bg
        # A skin (or a host) may hand a set, a list or a string for a style; the
        # cell keeps a frozenset so it stays hashable and internable.
        self.style = style if isinstance(style, frozenset) else frozenset(style or ())
        self.columns = cell_width(ch)
        self.attr = (fg, bg, self.style)
        self.key = (ch, fg, bg, self.style)

    @property
    def width(self) -> int:
        return self.columns

    @property
    def blank(self) -> bool:
        return self.ch == " " and self.fg is None and self.bg is None and not self.style

    def __eq__(self, other):
        if self is other:
            return True
        if not isinstance(other, Cell):
            return NotImplemented
        return self.key == other.key

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return "Cell(%r, fg=%r, bg=%r, style=%r)" % (
            self.ch, self.fg, self.bg, sorted(self.style))


def make_cell(ch: str, fg=None, bg=None, style=_NO_STYLE) -> Cell:
    """Interned cells: the diff then compares identities and equality is free."""
    if not isinstance(style, frozenset):
        style = frozenset(style or ())
    key = (ch, fg, bg, style)
    cell = _cell_cache.get(key)
    if cell is not None:
        return cell
    cell = Cell(ch, fg, bg, style)
    if len(_cell_cache) < MAX_CELL_CACHE:
        _cell_cache[key] = cell
    return cell


BLANK = make_cell(" ")


class Grid:
    """Offscreen buffer of cells. Coordinates are (column, row), 0-based.

    Out-of-range writes are clipped rather than raised: `blit` reports how many
    cells it wrote and how many were lost, so a skin can see it asked for more than
    the screen has.
    """

    def __init__(self, cols: int = DEFAULT_SIZE[0], rows: int = DEFAULT_SIZE[1]):
        self.cols, self.rows = clamp_size(cols, rows)
        self._rows = [[BLANK] * self.cols for _ in range(self.rows)]

    def resize(self, cols: int, rows: int) -> bool:
        """Reallocate; True when the shape really changed. Content is not kept."""
        cols, rows = clamp_size(cols, rows)
        if (cols, rows) == (self.cols, self.rows):
            return False
        self.cols, self.rows = cols, rows
        self._rows = [[BLANK] * cols for _ in range(rows)]
        return True

    def clear(self) -> None:
        for row in self._rows:
            for x in range(len(row)):
                row[x] = BLANK

    def copy_from(self, other: "Grid") -> None:
        """Adopt another grid's shape and cells (retiring a painted frame).

        The rows are copied, not aliased: if `prev` shared a list with the live
        grid, the next frame would diff a grid against itself and conclude the
        screen never changed.
        """
        if other.cols != self.cols or other.rows != self.rows:
            self.resize(other.cols, other.rows)
            return
        for y in range(self.rows):
            source = other._rows[y]
            # Copy only rows that differ, and always break an alias: sharing a list
            # with the live grid is how a diff would end up comparing a grid to
            # itself and reporting that nothing ever changes.
            if self._rows[y] is source or self._rows[y] != source:
                self._rows[y] = list(source)

    def put(self, x: int, y: int, cell: Cell) -> bool:
        """Place one cell; False when the coordinate is off-grid."""
        try:
            row = self._rows[y]
        except (IndexError, TypeError, ValueError):
            return False
        if x < 0 or x >= len(row):
            return False
        row[x] = cell
        return True

    def cell(self, x: int, y: int):
        try:
            row = self._rows[y]
        except (IndexError, TypeError, ValueError):
            return None
        if x < 0 or x >= len(row):
            return None
        return row[x]

    def line(self, y: int) -> str:
        """A row as text — for tests and for a host that wants to assert on output."""
        try:
            return "".join(cell.ch for cell in self._rows[y])
        except (IndexError, TypeError, ValueError):
            return ""

    def blit(self, x: int, y: int, text: str, fg=None, bg=None,
             style=_NO_STYLE) -> tuple[int, int]:
        """Write `text` at (x, y); returns (cells written, columns lost to clipping).

        Newlines drop a row and return to the starting column, tabs advance to the
        next tab stop, combining marks join the cell before them, and a double-width
        glyph also takes its right-hand column as a continuation cell so nothing can
        overwrite half of it.
        """
        if not text:
            return (0, 0)
        written = dropped = 0
        cx, cy = x, y
        base_col = base_row = -1
        for ch in text:
            if ch == "\n":
                cy += 1
                cx = x
                base_col = base_row = -1
                continue
            if ch == "\r" or ch in ("\x00", "\x07", "\x08"):
                continue
            if ch == "\t":
                pad = TAB_STOP - (cx % TAB_STOP) if cx >= 0 else TAB_STOP
                for _ in range(pad):
                    if self.put(cx, cy, make_cell(" ", fg, bg, style)):
                        written += 1
                        cx += 1
                    else:
                        dropped += 1
                base_col = base_row = -1
                continue
            width = char_width(ch)
            if width == 0:
                # Zero width: attach it to the base cell we just placed, otherwise
                # it would occupy a column of its own and shift the rest of the row.
                if base_col >= 0:
                    previous = self.cell(base_col, base_row)
                    if previous is not None and previous.ch:
                        self.put(base_col, base_row,
                                 make_cell(previous.ch + ch, previous.fg,
                                           previous.bg, previous.style))
                continue
            if cx < 0:
                # Off the left edge: burn columns until we are back on screen, so the
                # text still ends up where the skin meant it within the row.
                cx += width
                dropped += width
                continue
            if cx >= self.cols or cy < 0 or cy >= self.rows:
                dropped += width
                base_col = base_row = -1
                continue
            if width == 2 and cx + 1 >= self.cols:
                # Half a wide glyph is not a thing; lose it whole and say so.
                dropped += 2
                base_col = base_row = -1
                continue
            self.put(cx, cy, make_cell(ch, fg, bg, style))
            written += 1
            if width == 2:
                self.put(cx + 1, cy, make_cell("", fg, bg, style))
                written += 1
            base_col, base_row = cx, cy
            cx += width
        return (written, dropped)

    def fill(self, x: int, y: int, w: int, h: int, ch: str = " ", fg=None, bg=None,
             style=_NO_STYLE) -> int:
        """Fill a rectangle, clipped. Returns cells written."""
        if w <= 0 or h <= 0:
            return 0
        cell = make_cell(ch, fg, bg, style)
        written = 0
        for row in range(max(y, 0), min(y + h, self.rows)):
            target = self._rows[row]
            for col in range(max(x, 0), min(x + w, self.cols)):
                target[col] = cell
                written += 1
        return written

    def __repr__(self):
        return "Grid(%d, %d)" % (self.cols, self.rows)


# ---------------------------------------------------------------------------
# Painter — the surface a skin calls
# ---------------------------------------------------------------------------

# (top-left, horizontal, top-right, vertical, bottom-left, bottom-right).
# "minimal" is served by the rounded set: a minimal frame needs quadrant glyphs
# that cp1251-era conhost cannot draw, and promising a shape we cannot render on
# every target is worse than giving the closest one.
_BOX_CHARS = {
    "light": "┌─┐│└┘",
    "square": "┌─┐│└┘",
    "heavy": "┏━┓┃┗┛",
    "rounded": "╭─╮│╰╯",
    "minimal": "╭─╮│╰╯",
    "double": "╔═╗║╚╝",
    "ascii": "+-+|++",
    "none": "      ",
}
_BLANK_BOXES = ("none", "blank", "space")


class Painter:
    """What a skin gets handed every frame. Six methods, and that is the contract.

    Every one of them is exception-safe from the caller's point of view: a bad
    coordinate clips, a bad colour falls back to the terminal default, and the
    reason lands in `frame_warnings`. A skin can put anything through here and the
    worst case is a missing glyph plus a warning.
    """

    def __init__(self, grid: Grid | None = None, *, size=None, size_fn=None,
                 stream=None, depth: str | None = None, frame_warnings=None,
                 frame_index: int = 0):
        self._size_fn = size_fn or (lambda: terminal_size(stream))
        self._fixed_size = clamp_size(*size) if size else None
        self.depth = depth or color_support()
        self.grid = grid if grid is not None else Grid(*self.size())
        self.frame_warnings = frame_warnings if frame_warnings is not None else []
        self.warning_counts = {}
        self.frame_index = frame_index

    # -- plumbing ----------------------------------------------------------

    def warn(self, english: str, russian: str | None = None) -> None:
        """Collect one degraded-path note. The host shows these; the terminal never does."""
        message = L(english, russian if russian is not None else english)
        self.warning_counts[message] = self.warning_counts.get(message, 0) + 1
        if message not in self.frame_warnings and len(self.frame_warnings) < MAX_WARNINGS:
            self.frame_warnings.append(message)

    def clear_warnings(self) -> None:
        self.frame_warnings.clear()
        self.warning_counts.clear()

    def size(self) -> tuple[int, int]:
        if self._fixed_size is not None:
            return self._fixed_size
        try:
            return clamp_size(*self._size_fn())
        except Exception:
            return DEFAULT_SIZE

    @property
    def cols(self) -> int:
        return self.grid.cols

    @property
    def rows(self) -> int:
        return self.grid.rows

    # -- the contract ------------------------------------------------------

    def draw_text(self, x, y, text, color=None, bg=None, style=None) -> int:
        """Draw `text` with its top-left at (x, y); return the cells actually written.

        Cells, not bytes and not characters: "привет" is 6 (12 bytes in UTF-8), "你"
        is 2. Whatever did not fit the grid is reported by the shortfall and by a
        line in `frame_warnings`.
        """
        if text is None:
            return 0
        if not isinstance(text, str):
            kind = type(text).__name__
            self.warn("draw_text got %s instead of text; converted it" % kind,
                      "draw_text получил %s вместо текста; преобразовано" % kind)
            try:
                text = str(text)
            except Exception:
                return 0
        cleaned = strip_terminal(text)
        if cleaned != text:
            self.warn("control characters removed from drawn text",
                      "из текста убраны управляющие символы")
        known, dropped = normalize_style(style)
        if dropped:
            self.warn("ignored unknown style %s" % ", ".join(sorted(set(dropped))),
                      "игнорирован неизвестный стиль %s" % ", ".join(sorted(set(dropped))))
        fg = self._color_or_fallback(color)
        background = self._color_or_fallback(bg)
        try:
            x, y = int(x), int(y)
        except (TypeError, ValueError):
            self.warn("draw_text got a non-numeric position; drew at (0, 0) instead",
                      "draw_text получил нечисловую позицию; рисую в (0, 0)")
            x, y = 0, 0
        written, lost = self.grid.blit(x, y, cleaned, fg, background, known)
        if lost:
            self.warn("draw_text at (%d, %d) was clipped: %d cells did not fit" % (x, y, lost),
                      "draw_text в (%d, %d) обрезан: %d ячеек не поместилось" % (x, y, lost))
        return written

    def draw_box(self, x, y, w, h, border=None, fill=None, title=None,
                 charset: str = "light") -> None:
        """A rectangle of box-drawing glyphs, optionally titled and filled.

        `border` and `fill` take any colour value `color()` accepts; `charset`
        picks the glyph set ("light", "heavy", "rounded", "minimal", "double",
        "ascii", "none") — the frame slot of `ui/skin.py` in cell form. Nothing
        here raises: too small, off-grid or unknown, and it is a warning instead.
        """
        try:
            x, y, w, h = int(x), int(y), int(w), int(h)
        except (TypeError, ValueError):
            self.warn("draw_box got a non-numeric geometry; skipped",
                      "draw_box получил нечисловую геометрию; пропущено")
            return
        if w <= 1 or h <= 1:
            self.warn("draw_box %dx%d at (%d, %d) is too small to be a box; skipped" % (w, h, x, y),
                      "draw_box %dx%d в (%d, %d) слишком мал; пропущено" % (w, h, x, y))
            return
        name = str(charset or "light").strip().lower()
        frameless = name in _BLANK_BOXES
        if not frameless and name not in _BOX_CHARS:
            self.warn("unknown box charset %r; used light" % (charset,),
                      "неизвестный набор рамок %r; взят light" % (charset,))
            name = "light"
        glyphs = _BOX_CHARS.get(name, _BOX_CHARS["light"])
        fg = self._color_or_fallback(border)
        bg = self._color_or_fallback(fill)
        left, top = x, y
        right, bottom = x + w - 1, y + h - 1
        if right < 0 or bottom < 0 or left >= self.grid.cols or top >= self.grid.rows:
            # Nothing of this box can ever be seen; say so instead of silently
            # drawing into the void, which is how a skin ends up "not working".
            self.warn("draw_box at (%d, %d) is off-screen; nothing was drawn" % (x, y),
                      "draw_box в (%d, %d) вне экрана; ничего не нарисовано" % (x, y))
            return
        if not frameless:
            corner_tl, dash, corner_tr, bar, corner_bl, corner_br = glyphs[:6]
            self.grid.put(left, top, make_cell(corner_tl, fg, None, _NO_STYLE))
            self.grid.put(right, top, make_cell(corner_tr, fg, None, _NO_STYLE))
            self.grid.put(left, bottom, make_cell(corner_bl, fg, None, _NO_STYLE))
            self.grid.put(right, bottom, make_cell(corner_br, fg, None, _NO_STYLE))
            for col in range(left + 1, right):
                self.grid.put(col, top, make_cell(dash, fg, None, _NO_STYLE))
                self.grid.put(col, bottom, make_cell(dash, fg, None, _NO_STYLE))
            for row in range(top + 1, bottom):
                self.grid.put(left, row, make_cell(bar, fg, None, _NO_STYLE))
                self.grid.put(right, row, make_cell(bar, fg, None, _NO_STYLE))
        if bg is not None:
            # A fill is a colour request, not a glyph request: blank cells carrying
            # that background, so only the colour changes inside the frame.
            self.grid.fill(left + 1, top + 1, w - 2, h - 2, " ", None, bg, _NO_STYLE)
        if title and not frameless:
            label = strip_terminal(str(title))
            room = w - 4
            col, taken = left + 2, 0
            for ch in label:
                width = char_width(ch)
                if not width or taken + width > room:
                    break
                self.grid.put(col, top, make_cell(ch, fg, None, _NO_STYLE))
                if width == 2:
                    self.grid.put(col + 1, top, make_cell("", fg, None, _NO_STYLE))
                col += width
                taken += width

    def clear_region(self, x, y, w, h) -> None:
        """Blank a rectangle back to the grid's nothing; clipped, never raised."""
        try:
            x, y, w, h = int(x), int(y), int(w), int(h)
        except (TypeError, ValueError):
            self.warn("clear_region got a non-numeric geometry; skipped",
                      "clear_region получил нечисловую геометрию; пропущено")
            return
        if w <= 0 or h <= 0:
            self.warn("clear_region %dx%d at (%d, %d) is empty; nothing cleared" % (w, h, x, y),
                      "clear_region %dx%d в (%d, %d) пуст; ничего не очищено" % (w, h, x, y))
            return
        self.grid.fill(x, y, w, h, " ", None, None, _NO_STYLE)

    def get_terminal_size(self) -> tuple[int, int]:
        """(cols, rows) — 80x24 when there is no terminal to ask."""
        return self.size()

    def color_support(self) -> str:
        """"truecolor", "256" or "16", detected once and cached."""
        return self.depth

    def color(self, value) -> str:
        """A colour this terminal can show, as an opaque token.

        Accepts an (r, g, b) tuple, a "#rrggbb"/"#rgb" string, a palette name
        ("honey", "red", "bright_cyan") or a token this method returned earlier, and
        degrades it to the closest supported depth without telling the skin: the
        approximation is deterministic, so two runs look identical. The token is
        opaque — hand it back to `draw_text`/`draw_box`, do not parse it. "default"
        comes back for anything we cannot resolve.
        """
        return self._color_or_fallback(value) or "default"

    # -- internals ---------------------------------------------------------

    def _color_or_fallback(self, value):
        """Any colour value -> token for this depth, or None. Warns, never raises."""
        if value is None:
            return None
        try:
            token = degrade(value, self.depth)
        except Exception as exc:  # a colour object from a foreign library
            self.warn("colour %r could not be resolved (%s); used the terminal default"
                      % (value, exc),
                      "цвет %r не разрешился (%s); использован цвет по умолчанию"
                      % (value, exc))
            return None
        if token == "unknown":
            self.warn("unknown colour %r; used the terminal default" % (value,),
                      "неизвестный цвет %r; использован цвет по умолчанию" % (value,))
            return None
        return token

    # -- reading back ------------------------------------------------------

    def cell(self, x, y):
        """The cell at (x, y), or None off the grid. A skin that reacts to what is
        already drawn — glow over a letter, avoid a glyph — asks instead of
        remembering."""
        try:
            return self.grid.cell(int(x), int(y))
        except (TypeError, ValueError):
            return None

    # -- widgets: each one is a few `blit`s with the arithmetic done ---------

    def draw_bar(self, x, y, width, fraction, color=None, bg=None,
                 filled="█", empty="░") -> int:
        """A progress bar as `width` cells. Returns the cells written."""
        # The values go through unconverted: `draw_ramp` is where a width that is
        # not a number turns into a warning instead of a raised ValueError.
        return self.draw_ramp(x, y, width, color, color, filled=filled,
                              empty=empty, fraction=fraction, bg=bg, style=None)

    def draw_ramp(self, x, y, width, color_a, color_b, filled="█", empty=" ",
                  fraction: float = 1.0, bg=None, style=None) -> int:
        """`width` cells coloured from `color_a` to `color_b`.

        `fraction` says how far across the ramp is filled: a bar whose colour runs
        the whole width and whose length is the progress, which is one loop instead
        of a table the skin has to keep.
        """
        try:
            width, x, y = int(width), int(x), int(y)
            part = min(1.0, max(0.0, float(fraction)))
        except (TypeError, ValueError):
            self.warn("a bar was asked for with values it cannot use",
                      "полосу запросили со значениями, которые нельзя применить")
            return 0
        painted = 0
        full = int(round(width * part))
        for index in range(width):
            token = blend(color_a, color_b, index / max(1, width - 1))
            if index < full:
                cells, _lost = self.grid.blit(x + index, y, filled, fg=token, bg=bg,
                                              style=style)
            elif filled and empty != filled:
                cells, _lost = self.grid.blit(x + index, y, empty, fg=token, bg=bg,
                                              style=style)
            else:
                continue
            painted += cells
        return painted

    def draw_sparkline(self, x, y, width, values, color=None, bg=None) -> int:
        """The last `width` numbers as one row of block glyphs, tallest = max."""
        try:
            numbers = [float(v) for v in (values or [])][-int(width):]
        except (TypeError, ValueError):
            self.warn("a sparkline was asked for values that are not numbers",
                      "для графика переданы значения, которые не числа")
            return 0
        if not numbers:
            return 0
        top, bottom = max(numbers), min(numbers)
        span = top - bottom or 1.0
        written = 0
        for index, value in enumerate(numbers):
            step = int(round((value - bottom) / span * (len(SPARK_BLOCKS) - 1)))
            cells, _lost = self.grid.blit(int(x) + index, int(y), SPARK_BLOCKS[step],
                                          fg=color, bg=bg)
            written += cells
        return written

    def draw_ticker(self, x, y, width, text, phase: float = 0.0, color=None,
                    bg=None, style=None, gap: int = 3) -> int:
        """A marquee: `text` scrolls right to left through `width` cells.

        `phase` is anything that grows — the frame count, `time.monotonic()`, the
        loop's own number; it is taken modulo the travel, so a skin never has to
        remember where it stopped.
        """
        body = str(text or "")
        try:
            width, gap = int(width), max(1, int(gap))
        except (TypeError, ValueError):
            return 0
        if not body or width <= 0:
            return 0
        strip = body + " " * gap
        offset = int(float(phase) * len(strip)) % len(strip)
        window = (strip * (2 + width // len(strip)))[offset:offset + width]
        cells, _lost = self.grid.blit(int(x), int(y), window, fg=color, bg=bg,
                                      style=style)
        return cells


# ---------------------------------------------------------------------------
# widgets for text surfaces: markup, blends, easing, phases
#
# The four lines the skin owns as *strings* — status, spinner, thinking, stream —
# go through Rich, which reads its colour from markup. A skin that wants a
# shimmering status line should not have to build SGR codes or interpolate hex by
# hand every frame, so the arithmetic lives here once.
# ---------------------------------------------------------------------------

SPARK_BLOCKS = "▁▂▃▄▅▆▇█"

#: The curves a skin may name, and what each does with t in 0..1.
EASINGS = {
    "linear": lambda t: t,
    "in": lambda t: t * t,
    "out": lambda t: t * (2.0 - t),
    "in_out": lambda t: 2.0 * t * t if t < 0.5 else -1.0 + (4.0 - 2.0 * t) * t,
    "pulse": lambda t: abs(math.sin(t * math.pi)),
}


def blend(color_a, color_b, t: float) -> str:
    """A colour between two, as `#rrggbb`. `t` is clamped, and a name either side
    is resolved through the same table the painter uses."""
    a = token_rgb(color_a) or token_rgb("")
    b = token_rgb(color_b) or a
    part = min(1.0, max(0.0, float(t)))
    return "#%02x%02x%02x" % tuple(
        int(round(one + (other - one) * part)) for one, other in zip(a, b))


def ease(name: str, t: float) -> float:
    """`t` pushed through a named curve; an unknown name is linear, not an error."""
    try:
        value = min(1.0, max(0.0, float(t)))
    except (TypeError, ValueError):
        return 0.0
    return (EASINGS.get(str(name or "linear"), EASINGS["linear"]))(value)


def phase(clock, period: float = 1.0) -> float:
    """Where a cycle of `period` seconds we are, in 0..1 — wrapping included."""
    try:
        seconds, length = float(clock), max(0.001, float(period))
    except (TypeError, ValueError):
        return 0.0
    return (seconds % length) / length


def markup(text, fg=None, bg=None, style=None) -> str:
    """`text` wrapped in the Rich markup for one colour and/or style.

    Square brackets in the text itself are escaped: the answer a skin restyles is
    the model's bytes, and a `[bold]` inside a quoted piece of code is data, not an
    instruction to the terminal.
    """
    body = str(text if text is not None else "")
    if not body:
        return body
    words = [item for item in (str(fg or "").strip(), str(bg or "").strip(),
                               str(style or "").strip()) if item]
    # The backslash goes in through a constant: a backslash inside an f-string
    # expression is a syntax error on the 3.10 this project still claims to run on.
    body = body.replace("[", "\\[")
    if not words:
        return body
    return "[" + " ".join(words) + "]" + body + "[/]"


def ramp_markup(text, color_a, color_b, style: str = "") -> str:
    """Every character its own colour between two — a shimmer or a wave in one call."""
    body = str(text or "")
    if not body:
        return ""
    if len(body) == 1:
        return markup(body, blend(color_a, color_b, 0.5), style=style)
    return "".join(markup(word, blend(color_a, color_b, index / (len(body) - 1)),
                          style=style) for index, word in enumerate(body))


def bar_markup(fraction, width: int = 10, filled: str = "█", empty: str = "░",
               fg=None, bg=None, label: str = "") -> str:
    """A bar as text, for the line a terminal draws with Rich instead of a grid."""
    try:
        cells = max(0, int(width))
        part = min(1.0, max(0.0, float(fraction)))
    except (TypeError, ValueError):
        return str(label or "")
    full = int(round(cells * part))
    strip = filled * full + empty * (cells - full)
    return markup(strip + (f" {label}" if label else ""), fg, bg=bg)


def spark_markup(values, fg=None, width: int = 0) -> str:
    """A row of block glyphs for the last few numbers — tokens, timings, retries."""
    try:
        numbers = [float(v) for v in (values or [])]
    except (TypeError, ValueError):
        return ""
    if not numbers:
        return ""
    if width and len(numbers) > int(width):
        numbers = numbers[-int(width):]
    top, bottom = max(numbers), min(numbers)
    span = top - bottom or 1.0
    row = "".join(SPARK_BLOCKS[int(round((value - bottom) / span * (len(SPARK_BLOCKS) - 1)))]
                  for value in numbers)
    return markup(row, fg)


def ticker_markup(text, width: int, phase_value: float = 0.0, gap: int = 3,
                  fg=None) -> str:
    """A scrolling one-line strip of text, cut to `width` visible cells."""
    body = str(text or "")
    try:
        cells, gap = max(0, int(width)), max(1, int(gap))
    except (TypeError, ValueError):
        return body[:max(0, int(width))] if width else body
    if not body or cells <= 0:
        return ""
    if text_cells(body) <= cells:
        return markup(body, fg)
    strip = body + " " * gap
    offset = int(float(phase_value or 0.0) * len(strip)) % len(strip)
    window = (strip * (2 + cells // max(1, len(strip))))[offset:offset + cells]
    return markup(window, fg)


def blink(on: bool, text, off=None) -> str:
    """The text, or nothing. `off=None` keeps the width with spaces, which is what
    stops a status line from jumping when a dot appears in it."""
    if on:
        return str(text or "")
    if off is None:
        return " " * text_cells(str(text or ""))
    return str(off or "")



_ENABLE_VIRTUAL_TERMINAL = 0x0004


def _enable_windows_vt(stream):
    """Ask conhost for ANSI processing; return the original mode if we changed it.

    Without this, plain conhost prints the escape sequences as text and a "render
    engine" is a typewriter of garbage. Windows Terminal already has it on, and a
    redirected stream has no mode to change, so both quietly return None.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        fd = stream.fileno()
        if fd not in (1, 2):
            return None
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11 if fd == 1 else -12)
        mode = wintypes.DWORD()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return None
        original = int(mode.value)
        if original & _ENABLE_VIRTUAL_TERMINAL:
            return None
        if not kernel32.SetConsoleMode(handle, original | _ENABLE_VIRTUAL_TERMINAL):
            return None
        return original
    except Exception:
        # No ctypes, no kernel32, a stream without a fileno (a StringIO in a test):
        # we simply do not have VT, and the loop keeps running.
        return None


def _restore_windows_vt(stream, original):
    if original is None or os.name != "nt":
        return
    try:
        import ctypes

        fd = stream.fileno()
        if fd not in (1, 2):
            return
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11 if fd == 1 else -12)
        kernel32.SetConsoleMode(handle, original)
    except Exception:
        pass


class Screen:
    """Terminal lifecycle: alternate screen in and out, cursor off and on, clear.

    Every method is idempotent and remembers what it asked for, so a double
    `leave()` cannot drop the user into a blank screen and `teardown()` after an
    exception restores exactly what is still owed. This is also the single door
    through which escape codes may leave the process: a stream that is not a
    terminal is refused here, once, for everybody.
    """

    ALT_ENTER = "\x1b[?1049h"
    ALT_LEAVE = "\x1b[?1049l"
    CURSOR_ON = "\x1b[?25h"
    CURSOR_OFF = "\x1b[?25l"
    CLEAR = "\x1b[2J\x1b[H"
    RESET = "\x1b[0m"

    def __init__(self, stream=None, *, size=None, size_fn=None, tty=None):
        self.stream = stream if stream is not None else sys.stdout
        if tty is None:
            try:
                tty = bool(self.stream.isatty())
            except Exception:
                tty = False
        self.is_tty = bool(tty)
        self._fixed_size = clamp_size(*size) if size else None
        self._size_fn = size_fn or (lambda: terminal_size(self.stream))
        self.alt = False
        self.cursor_hidden = False
        self.cleared = 0
        self.bytes_written = 0
        self.write_errors = 0
        self._vt_original = None

    def write(self, payload: str) -> int:
        """Raw bytes to the terminal. A non-TTY gets nothing, ever. Returns bytes sent."""
        if not payload or not self.is_tty:
            return 0
        data = payload.encode("utf-8", "replace")
        try:
            buffer = getattr(self.stream, "buffer", None)
            if buffer is not None:
                # The text layer may hold pending output (a banner, a prompt): it
                # has to land first, or our frame arrives out of order.
                try:
                    self.stream.flush()
                except Exception:
                    pass
                buffer.write(data)
                buffer.flush()
            else:
                self.stream.write(payload)
                self.stream.flush()
        except Exception:
            # A terminal that vanished mid-frame (closed tab, pulled USB serial)
            # should end the loop, not the process. The caller watches the counters.
            self.write_errors += 1
            return 0
        self.bytes_written += len(data)
        return len(data)

    def size(self) -> tuple[int, int]:
        if self._fixed_size is not None:
            return self._fixed_size
        try:
            return clamp_size(*self._size_fn())
        except Exception:
            return DEFAULT_SIZE

    def enter(self) -> bool:
        """Alternate screen on, cleared, style default. Second call does nothing."""
        if self.alt or not self.is_tty:
            return False
        self._vt_original = _enable_windows_vt(self.stream)
        self.alt = True
        # The clear is not decoration: `1049h` is only specified to *switch*
        # buffers, and our model starts empty, so the screen has to be empty too.
        self.write(self.ALT_ENTER + self.RESET + self.CLEAR)
        self.cleared += 1
        return True

    def leave(self) -> bool:
        """Alternate screen off. Safe to call when we never entered, or twice."""
        if not self.alt:
            return False
        self.alt = False
        self.write(self.ALT_LEAVE)
        _restore_windows_vt(self.stream, self._vt_original)
        self._vt_original = None
        return True

    def hide_cursor(self) -> bool:
        if self.cursor_hidden or not self.is_tty:
            return False
        self.cursor_hidden = True
        self.write(self.CURSOR_OFF)
        return True

    def show_cursor(self) -> bool:
        if not self.cursor_hidden:
            return False
        self.cursor_hidden = False
        self.write(self.CURSOR_ON)
        return True

    def clear(self) -> bool:
        if not self.is_tty:
            return False
        self.cleared += 1
        self.write(self.CLEAR)
        return True

    def reset_style(self) -> bool:
        if not self.is_tty:
            return False
        self.write(self.RESET)
        return True

    def teardown(self, show_cursor: bool = True, leave_alt: bool = True) -> None:
        """Give the terminal back: default style, visible cursor, primary screen."""
        self.reset_style()
        if show_cursor:
            self.show_cursor()
        if leave_alt:
            self.leave()

    def __repr__(self):
        return "Screen(tty=%s, alt=%s, hidden=%s)" % (
            self.is_tty, self.alt, self.cursor_hidden)


# ---------------------------------------------------------------------------
# Emitter — the diff
# ---------------------------------------------------------------------------

# The strategy in one line: walk the grid row by row; a *run* is a maximal stretch
# of changed cells sharing one attribute set; jump over unchanged cells with the
# shortest sequence that gets there.
#
# Row-major beats a longest-common-subsequence pass for a text UI because nearly
# every frame differs in one row (a spinner tick, one more word of an answer), and
# it makes a width miscalculation self-correcting: a run whose glyphs were not all
# one-column opens the next one with an absolute move instead of a relative one, so
# a wrong guess cannot push the rest of the screen out of place.
class Emitter:
    """Turn two grids into the bytes that make the screen match the new one."""

    def __init__(self, screen: Screen | None = None, depth: str | None = None):
        self.screen = screen
        self.depth = depth or color_support()
        self.total_runs = 0
        self.total_moves = 0
        self.total_cells = 0

    @staticmethod
    def _needs_write(now: Cell, before: Cell) -> bool:
        """Does this column need bytes? False for a cell the screen already shows.

        A blank over a blank is nothing to do. A blank over a wide glyph, or over
        that glyph's continuation column, *is* something to do: without it the old
        double-width character keeps sitting there, because one space erases one
        cell and the glyph occupied two.
        """
        if now is before:
            return False
        if now.key == before.key:
            return False
        return bool(now.ch) or before.ch == "" or before.columns == 2

    def render(self, prev: Grid, cur: Grid, full: bool = False) -> dict:
        """The payload for one frame. Nothing is written here — see `emit`."""
        if prev.cols != cur.cols or prev.rows != cur.rows:
            # A shape mismatch is not a diff: comparing rows of different lengths is
            # how a resize becomes a torn screen.
            full = True
        chunks = []
        runs = moves = cells = 0
        cleared = False
        width, height = cur.cols, cur.rows
        if full:
            chunks.append("\x1b[0m" + Screen.CLEAR)
            cleared = True
            # Compare against one shared blank row instead of the previous grid: a
            # repaint of a cleared screen is exactly "everything that is not blank".
            blank_row = [BLANK] * width
            before_rows = [blank_row] * height
        else:
            before_rows = prev._rows
        # The terminal is at the default style at the top of every frame — `enter`
        # put it there and every frame ends by resetting if it changed attributes —
        # so this is a fact rather than a hope, and no frame inherits another's SGR.
        state = None
        cursor = None  # (column, row) the pen sits at, or None when unknown
        trusted = False  # whether `cursor` may be jumped from relatively
        for y in range(height):
            row = cur._rows[y]
            before_row = before_rows[y]
            if row is before_row or row == before_row:
                # A whole row that already matches: list equality compares element by
                # element in C, with identity first for our interned cells. This is
                # the common frame in a chat UI — one row moved, 23 did not.
                continue
            x = 0
            while x < width:
                cell = row[x]
                # A run never *starts* on a continuation cell: the glyph that owns it
                # is written one column earlier and already covers this one.
                if not cell.ch or not self._needs_write(cell, before_row[x]):
                    x += 1
                    continue
                attr = cell.attr
                chars = [cell.ch]
                advance = cell.columns
                start = x
                x += 1
                while x < width:
                    nxt = row[x]
                    if nxt.attr != attr or not self._needs_write(nxt, before_row[x]):
                        break
                    chars.append(nxt.ch)
                    advance += nxt.columns
                    x += 1
                columns = x - start
                text = "".join(chars)
                # Cheapest way onto the run: within a row of all-single-width cells a
                # relative jump is 3-5 bytes against an absolute move's 6-9; once a
                # wide glyph is involved we stop trusting our own arithmetic and pay
                # for the absolute move, which cannot drift.
                if trusted and cursor is not None and cursor[1] == y and start >= cursor[0]:
                    gap = start - cursor[0]
                    if gap == 1:
                        chunks.append("\x1b[C")
                        moves += 1
                    elif gap > 1:
                        chunks.append("\x1b[%dC" % gap)
                        moves += 1
                else:
                    chunks.append("\x1b[%d;%dH" % (y + 1, start + 1))
                    moves += 1
                if attr != state:
                    chunks.append(sgr(attr[0], attr[1], attr[2], self.depth))
                    state = attr if (attr[0] or attr[1] or attr[2]) else None
                chunks.append(text)
                cursor = (start + columns, y)
                # Trust our own arithmetic only while every cell in the run was one
                # column wide: after a double-width glyph the pen is somewhere we are
                # guessing at, so the next run pays for an absolute move instead.
                trusted = advance == columns
                runs += 1
                cells += columns
        if state is not None:
            chunks.append("\x1b[0m")
        payload = "".join(chunks)
        return {
            "payload": payload,
            "bytes": len(payload.encode("utf-8", "replace")),
            "runs": runs,
            "moves": moves,
            "cells": cells,
            "full": bool(full),
            "cleared": cleared,
        }

    def emit(self, prev: Grid, cur: Grid, full: bool = False) -> dict:
        """Render and send one frame; `bytes` becomes what the stream accepted."""
        result = self.render(prev, cur, full)
        if self.screen is not None and result["payload"]:
            result["bytes"] = self.screen.write(result["payload"])
        if result["payload"]:
            self.total_runs += result["runs"]
            self.total_moves += result["moves"]
            self.total_cells += result["cells"]
        return result


# ---------------------------------------------------------------------------
# FrameLoop — the clock
# ---------------------------------------------------------------------------

class FrameLoop:
    """Paint, diff, write, wait. Bounded, clamped, and safe to stop by surprise.

    `paint` is the skin's callable, invoked as ``paint(painter)`` — one positional
    argument, because a second one would make every skin guess its own arity. The
    painter carries `frame_index`, `cols` and `rows` for a skin that needs them,
    and the frame it draws is not shown until it is complete.
    """

    def __init__(self, paint=None, *, stream=None, screen: Screen | None = None,
                 painter: Painter | None = None, size=None, size_fn=None,
                 depth: str | None = None, use_alt_screen: bool = True,
                 hide_cursor: bool = True, max_paint_errors: int = 3):
        self.stream = stream if stream is not None else sys.stdout
        self._fixed_size = clamp_size(*size) if size else None
        self._size_fn = size_fn or (lambda: self._fixed_size or terminal_size(self.stream))
        self.screen = screen if screen is not None else Screen(
            self.stream, size=size, size_fn=self._size_fn)
        self.depth = depth or (painter.depth if painter is not None else color_support())
        if painter is not None:
            # A host that built its own Painter owns its grid: the loop adopts it,
            # because that is the painter the skin is going to be handed.
            self.painter = painter
            if depth:
                painter.depth = depth
            self.grid = painter.grid
        else:
            self.grid = Grid(*self._current_size())
            self.painter = Painter(self.grid, size_fn=self._current_size,
                                   depth=self.depth, stream=self.stream)
        self.prev = Grid(self.grid.cols, self.grid.rows)
        self.emitter = Emitter(self.screen, self.painter.depth)
        self.paint = paint
        self.use_alt_screen = use_alt_screen
        self.hide_cursor = hide_cursor
        self.max_paint_errors = max(1, int(max_paint_errors))
        self.fps = float(DEFAULT_FPS)
        self.budget = 1.0 / self.fps
        self.running = False
        self.frames = 0
        self.emitted_frames = 0
        self.skipped_frames = 0
        self.overrun_frames = 0
        self.resizes = 0
        self.paint_errors = 0
        self.cells_written = 0
        self.bytes_written = 0
        self.last_frame_ms = 0.0
        self.total_ms = 0.0
        self.degraded_reason = None
        self._stop = threading.Event()
        self._notified = False

    # -- state -------------------------------------------------------------

    @property
    def frame_warnings(self) -> list:
        """Why a frame looks different from what the skin asked for. Bounded."""
        return self.painter.frame_warnings

    @property
    def warning_counts(self) -> dict:
        """How often each warning happened, so a flood is visible as a flood."""
        return self.painter.warning_counts

    @property
    def is_tty(self) -> bool:
        return self.screen.is_tty

    def stop(self) -> None:
        """Ask the loop to end. Thread-safe, and fine to call before or after `run`."""
        self._stop.set()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def set_paint(self, paint) -> None:
        self.paint = paint

    def clear_warnings(self) -> None:
        self.painter.clear_warnings()

    def stats(self) -> dict:
        """Everything the loop knows about its own run, for the host to surface."""
        return {
            "frames": self.frames,
            "emitted": self.emitted_frames,
            "skipped": self.skipped_frames,
            "overrun": self.overrun_frames,
            "resizes": self.resizes,
            "paint_errors": self.paint_errors,
            "cells": self.cells_written,
            "bytes": self.bytes_written,
            "fps": self.fps,
            "budget": self.budget,
            "last_frame_ms": round(self.last_frame_ms, 3),
            "total_ms": round(self.total_ms, 3),
            "tty": self.is_tty,
            "degraded": self.degraded_reason,
            "warnings": len(self.frame_warnings),
        }

    def __repr__(self):
        return "FrameLoop(fps=%s, tty=%s, frames=%d)" % (self.fps, self.is_tty, self.frames)

    # -- size --------------------------------------------------------------

    def _current_size(self) -> tuple[int, int]:
        if self._fixed_size is not None:
            return self._fixed_size
        try:
            return clamp_size(*self._size_fn())
        except Exception:
            return DEFAULT_SIZE

    # -- the loop ----------------------------------------------------------

    def run(self, fps=DEFAULT_FPS, max_frames=None) -> dict:
        """Run the cycle; returns `stats()`. Never raises, whatever a skin does."""
        self.fps = clamp_fps(fps)
        self.budget = 1.0 / self.fps
        self._stop.clear()
        try:
            max_frames = None if max_frames is None else max(0, int(max_frames))
        except (TypeError, ValueError):
            self.painter.warn("max_frames was not a number; running without a limit",
                              "max_frames — не число; работаю без ограничения")
            max_frames = None

        if not self.is_tty:
            # A pipe: no escapes, no painting, one honest sentence. `FrameLoop` in
            # this mode is a no-op that says so once, and the text UI takes over.
            self.degraded_reason = L(
                "renderer: stdout is not a terminal, so nothing is drawn "
                "(frame loop off; the text interface is used instead)",
                "рендерер: stdout не терминал, ничего не рисуем "
                "(цикл кадров выключен; работает текстовый интерфейс)")
            self._announce_once()
            return self.stats()

        if self.paint is None or not callable(self.paint):
            self.degraded_reason = L("renderer: there is no skin to paint with",
                                     "рендерер: некому рисовать")
            self.painter.warn("FrameLoop has no paint callback; nothing ran",
                              "у FrameLoop нет колбэка отрисовки; ничего не выполнено")
            self._announce_once()
            return self.stats()

        self.running = True
        started = time.monotonic()
        next_due = started
        # `max_frames` bounds this call, not the object's whole life: a host that
        # restarts the loop (a skin switch, a `/redraw`) must get its N frames.
        frames_before_run = self.frames
        # The Screen's error count is cumulative; this run only cares about the
        # writes it lost itself, so a restart after a transient failure works.
        write_errors_before_run = self.screen.write_errors
        errors_in_a_row = 0
        # Without the alternate screen there was nothing to clear on the way in, so
        # whatever the terminal was showing before is still there: the first frame
        # has to repaint the whole grid rather than diff against a blank model.
        repaint_whole = not self.use_alt_screen
        try:
            if self.use_alt_screen:
                self.screen.enter()
            if self.hide_cursor:
                self.screen.hide_cursor()
            while not self._stop.is_set():
                if max_frames is not None and self.frames - frames_before_run >= max_frames:
                    break
                frame_started = time.monotonic()
                cols, rows = self._current_size()
                if (cols, rows) != (self.grid.cols, self.grid.rows):
                    # A resize between frames: cells from the old shape mean nothing
                    # in the new one, so the diff is abandoned and the whole screen
                    # is repainted after a clear. Anything else prints a torn frame.
                    self.grid.resize(cols, rows)
                    self.prev.resize(cols, rows)
                    self.painter.grid = self.grid
                    self.resizes += 1
                    repaint_whole = True
                self.painter.frame_index = self.frames
                painted = self._paint_once()
                self.frames += 1
                errors_in_a_row = 0 if painted else errors_in_a_row + 1
                if painted:
                    result = self.emitter.emit(self.prev, self.grid, repaint_whole)
                    self.prev.copy_from(self.grid)
                    if result["payload"]:
                        # Only a frame that sent bytes counts as emitted: a steady
                        # screen is painted every beat and sends nothing, which is
                        # the whole point of the diff.
                        self.emitted_frames += 1
                        self.bytes_written += result["bytes"]
                        self.cells_written += result["cells"]
                    repaint_whole = False
                    if self.screen.write_errors:
                        # The terminal went away. Say so, and stop rather than spin.
                        self.painter.warn(
                            "the terminal stopped accepting output after %d frames"
                            % self.frames,
                            "терминал перестал принимать вывод после %d кадров"
                            % self.frames)
                        break
                elif errors_in_a_row >= self.max_paint_errors:
                    # Three bad frames in a row is not one unlucky skin; stop with
                    # the screen restored rather than paint nothing forever.
                    self.painter.warn(
                        "the skin failed %d frames in a row; the frame loop "
                        "stopped rather than freezing the screen" % errors_in_a_row,
                        "скин упал %d кадров подряд; цикл остановлен, чтобы экран не завис"
                        % errors_in_a_row)
                    break
                elapsed = time.monotonic() - frame_started
                self.last_frame_ms = elapsed * 1000.0
                self.total_ms += self.last_frame_ms
                if elapsed > self.budget:
                    self.overrun_frames += 1
                next_due += self.budget
                now = time.monotonic()
                if now >= next_due:
                    # Behind the clock: drop the frames we cannot make up instead of
                    # queueing them. A queue here shows the user last second's answer
                    # and reads as a freeze. `skipped_frames` is what they can see.
                    missed = int((now - next_due) // self.budget)
                    if missed > 0:
                        next_due += missed * self.budget
                        self.skipped_frames += missed
                else:
                    # Event.wait, not time.sleep: stop() from another thread lands
                    # immediately instead of after the rest of the beat.
                    self._stop.wait(next_due - now)
        except KeyboardInterrupt:
            self.painter.warn("the frame loop was interrupted", "цикл кадров прерван")
        except Exception as exc:
            # The screen and the emitter swallow their own failures, so reaching
            # this is a bug in the engine: it must end the loop, not the REPL.
            self.painter.warn("the renderer hit an internal error and stopped: %s" % exc,
                              "внутри рендерера ошибка; цикл кадров остановлен: %s" % exc)
        finally:
            self.running = False
            self._restore()
        return self.stats()

    def _paint_once(self) -> bool:
        """One call into the skin. False throws the frame away: it never reaches the
        terminal half-drawn, and the primary screen still comes back."""
        try:
            self.paint(self.painter)
            return True
        except Exception as exc:
            self.paint_errors += 1
            self.painter.warn("the skin raised %s at frame %d; the frame was dropped"
                              % (type(exc).__name__, self.frames),
                              "скин вызвал %s в кадре %d; кадр отброшен"
                              % (type(exc).__name__, self.frames))
            return False

    def _restore(self) -> None:
        """Cursor visible, alternate screen left, style reset — on every exit path."""
        try:
            self.screen.teardown(show_cursor=True, leave_alt=True)
        except Exception:
            pass

    def _announce_once(self) -> None:
        """Say the degraded thing out loud, once, and only where it cannot harm."""
        if self._notified or not self.degraded_reason:
            return
        self._notified = True
        try:
            err = sys.stderr
            if err is not None and err.isatty():
                err.write(self.degraded_reason + "\n")
                err.flush()
        except Exception:
            pass
