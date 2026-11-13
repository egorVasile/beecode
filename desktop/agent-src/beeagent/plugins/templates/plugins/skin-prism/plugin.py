"""`skin-prism` — the whole interface under one spectrum that never stops moving.

What this pack takes over, and what it does with each piece:

* `banner` — the "BEECODE" logo, recoloured, never redrawn: the shape is read off
  the `default` the host hands over, and only its colours are ours. Two clocks run
  on it at once — a crest that crosses the letters every `PRISM_WAVE_SECONDS`, and
  a slower rotation of the palette itself — so the frame at t+3s is not the frame
  at t+0s with two colours swapped. On a narrow box the word is *squeezed* — the
  gutters between the letters first, then the horizontal stretch folded out of the
  letters themselves, 76 columns to 70 to 41 to 35 — rather than reshaped, which is
  what keeps it readable instead of turning it into noise.
* `frame` — the colour of a panel's border, per role. Every role sits at its own
  point of one shared cycle (`answer` at the start, `tool` a sixth in, `output` a
  third, `prompt` at the half, `picker` past two thirds), so four panels on one
  screen read as one prism across them rather than as a strobe. The `error` role is
  kept out of the spectrum on purpose: it walks a short red cycle. A border that
  drifts through the rainbow eventually paints a failure in the colour of a
  success, and that is not decoration, it is a bug with good timing.
* `answer` — the reply, restyled as markup. See the two rules below.
* `status`, `spinner`, `thinking` — one line each, phased by the clock the host
  hands over, ASCII glyphs only: those three lines reach terminals that cannot
  draw a block character, and a state line that prints `?` is worse than a plain
  one.
* `hud` — two reserved rows through the painter: the state and its numbers on top,
  the spectrum strip and what is running below it.

Two rules this pack is built around, because it is asked to be beautiful *and* to
be trusted with the answer:

**Prism paints the markdown; it does not consume it.** `skin-shimmer` parses the
reply into a laid-out block, which is how it turns `- item` into a bullet and drops
the `#` of a heading: the words survive, the model's characters do not. This pack
keeps every character the model wrote — the hashes, the fence ticks, the `**` — and
puts the colour *on* them: a heading's hashes go dim while its letters carry the
spectrum, a bullet's dash is painted rather than replaced, a fenced block is left
exactly as typed. So the guarantee is the strong kind, the kind that needs no
judgement call: take the tags off and the text is the input, character for
character. It is also what makes the pass linear — no parser, no layout, no
re-typesetting of an answer that is still growing.

**The unit of cost is the tag, not the pixel.** A skin that answers in markup is
handed to `Text.from_markup`, and that parser costs roughly 25 microseconds *per
tag pair* on this machine — a 2800-character block with 280 of them costs 8 ms,
and the same text in ten tags costs 0.3 ms. So nothing here paints a cell at a
time: consecutive lines that share a colour leave the block as one tag spanning
their newlines, the logo merges the columns one colour band covers, and the HUD
strip is eight long runs instead of sixty `draw_ramp` cells (measured 8x the cost of
the same pixels as runs). The answer is therefore tinted in `PRISM_BANDS` steps down
the block rather than one hue per line — which is also what it *looks* like, a
spectrum ladder — and a saturated rail on every line was the first thing that had to
go for it: the rail takes its line's tint and reads as a rule because of the glyph,
not because of the colour. The ramp tables are built once, at import, so a cell
costs a lookup and never a blend.

Motion comes from the numbers the host passes in: `seconds` and `dt`. Nothing here
reads a clock, so a test can advance a whole turn without waiting for one, and
`on_answer`, which is handed no clock at all, takes its phase from the seconds the
frame loop already gave it. Where the renderer reports no colour, no tags are
emitted at all: the same words, the same margin, plain text.

One hazard is worth naming, because it is invisible from this file: `Rich` reads
`:snake:` in a markup string and hands back a snake — which deletes characters from
the model's answer, the one thing this surface may not do. `prism_hold` cuts every
`:word:` shape across a tag boundary (two chunks, one style): the plain text is
unchanged and the short code stays the model's own letters.
"""
import beeagent.core.renderer as R
import beeagent.core.skins as S
import dataclasses

NAME = "prism"
PACK = "skin-prism"
DESCRIPTION = "Prism skin: a looping logo cycle, a per-role frame spectrum, an answer painted in light"

#: The seven pieces this pack asks to own. `stream` is not among them: that surface
#: is the model's bytes on their way to the log, and a skin that rewrites them
#: hides the answer behind its own decoration.
SURFACES = ("banner", "frame", "answer", "status", "spinner", "thinking", "hud")

#: Two rows is what the strip is laid out for. The host clamps at four and cuts
#: without asking, and a picture laid out for four rows given two is a bug.
HUD_ROWS = max(1, min(2, S.HUD_MAX_ROWS))

# ------------------------------------------------------------------ the palette --

#: Violet to rose and back to violet: a closed cycle, so the ramp has no seam where
#: "the end of the gradient" would otherwise show up as a flicker.
PRISM_STOPS = ("#7b5cff", "#2f9bff", "#21e6ff", "#45e0a8",
               "#ffd166", "#ff8a5c", "#ff4d8d", "#d44dff")
PRISM_RAMP_LEN = 64
PRISM_ERROR_STOPS = ("#ff3b3b", "#ff6f43", "#e01f5f", "#ff1f3d", "#c9243a")
PRISM_ERROR_LEN = 16

PRISM_SLATE = "#c7d3de"      # the readable middle a body line is tinted toward
PRISM_INK = "#7d8794"        # a marker that should not compete with its own line
PRISM_HOT = "#f2fbff"        # the leading edge of a wave, the crest of a band
PRISM_CODE = "#9fc3d8"       # fenced code: calm, cool, never animated
PRISM_DEEP = "#141a22"       # the label's background


def prism_cycle(stops, count):
    """`count` colours around a closed loop of `stops`, eased between neighbours."""
    total = len(stops)
    colours = []
    for index in range(count):
        position = index * total / float(count)
        here = int(position) % total
        there = (here + 1) % total
        colours.append(R.blend(stops[here], stops[there],
                               R.ease("in_out", position - int(position))))
    return tuple(colours)


#: Every table below is built once, here, and only indexed after that. That is the
#: whole budget story: a cell costs a lookup, never a blend.
PRISM_RAMP = prism_cycle(PRISM_STOPS, PRISM_RAMP_LEN)
PRISM_ERROR = prism_cycle(PRISM_ERROR_STOPS, PRISM_ERROR_LEN)
#: Body text: the spectrum, 72% of the way to slate. You can read a paragraph in
#: it, which is the difference between a shimmer on the answer and a light show.
PRISM_SOFT = tuple(R.blend(colour, PRISM_SLATE, 0.72) for colour in PRISM_RAMP)
PRISM_GLOW = tuple(R.blend(colour, PRISM_HOT, 0.42) for colour in PRISM_RAMP)
PRISM_BOLD = "bold "         # Rich takes "bold <hex>" as one style, order aside
PRISM_CLOSE = "[/]"
PRISM_BRACKET = "\\["        # the one character Rich would otherwise read as a tag


def prism_tags(colours, prefix: str = ""):
    """Pre-opened markup tags, so painting a cell is two concatenations."""
    return tuple("[" + prefix + colour + "]" for colour in colours)


PRISM_OPEN = prism_tags(PRISM_RAMP, PRISM_BOLD)
#: The logo's whole alphabet of tags: 64 spectrum steps, then the hot crest, then
#: "" for a column that holds no ink. One lookup per cell, never a build.
PRISM_CELL_TAGS = PRISM_OPEN + ("[" + PRISM_HOT + "]",) + ("",)
PRISM_HOT_CELL = PRISM_RAMP_LEN
PRISM_BLANK_CELL = PRISM_RAMP_LEN + 1

# -------------------------------------------------------------------- the clocks --

#: A crest crosses the word in this many seconds...
PRISM_WAVE_SECONDS = 3.2
#: ...while the palette itself rotates in this many. Deliberately not a multiple of
#: the first: that is what stops a looping animation reading as a two-frame blink.
PRISM_DRIFT_SECONDS = 11.0
PRISM_FRAME_SECONDS = 9.5    # one turn of the border cycle every role shares
PRISM_TRAVEL_SECONDS = 6.4   # one sweep of the light band down an answer
PRISM_TRAVEL_STEPS = 24      # how finely that sweep is quantised
PRISM_SPINNER_SECONDS = 1.6
PRISM_WAVE_WAVES = 1.75      # crests across the word at one instant
PRISM_ROW_TILT = 0.055       # the wave leans: each row trails the one above
PRISM_FLASH = 0.045          # the leading edge, in turns of the wave

#: How many colours a wave is cut into. Twelve is enough for a 76-column word to
#: read as a travelling band, and it is what lets whole runs of columns share one
#: tag: see the cost note in the header.
PRISM_LEVELS = 12
PRISM_LEVEL_STEP = max(1, PRISM_RAMP_LEN // PRISM_LEVELS)
#: Steps of the spectrum down a finished answer. Same reason, same effect.
PRISM_BANDS = 10
PRISM_RUNS = 8               # how many draw calls the HUD strip is cut into

#: Where each panel role sits on the one cycle, as a fraction of the ramp.
PRISM_ROLE_SPOTS = {"answer": 0.0, "tool": 0.17, "output": 0.34, "prompt": 0.52,
                    "picker": 0.69, "menu": 0.69, "hud": 0.44, "warning": 0.05}
PRISM_SHIFT = dict((word, int(spot * PRISM_RAMP_LEN))
                   for word, spot in PRISM_ROLE_SPOTS.items())
PRISM_ERROR_ROLES = ("error", "fail", "failed", "danger", "denied")

# ---------------------------------------------------------------------- the logo --

#: The same 5x5 shapes `ui/components.py` ships, so a skin that has to draw the
#: word itself draws *this* word.
PRISM_PIXELS = {
    "B": ("████ ", "█   █", "████ ", "█   █", "████ "),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
    "C": (" ████", "█    ", "█    ", "█    ", " ████"),
    "O": (" ███ ", "█   █", "█   █", "█   █", " ███ "),
    "D": ("████ ", "█   █", "█   █", "█   █", "████ "),
    " ": ("     ", "     ", "     ", "     ", "     "),
}
PRISM_WORD = "BEECODE"
PRISM_LOGO_SCALE = 2
PRISM_LOGO_ROWS = 5
PRISM_GUTTER = 2             # the answer's left margin, in cells

#: Narrowest screen the two-row strip is worth reserving.
PRISM_MIN_HUD_COLS = 36
#: The live path draws the tail a viewer can actually see — both because a
#: forty-page answer re-painted three times a second costs the user their own
#: typing, and because this is what makes the cost of a frame independent of how
#: long the answer has grown.
PRISM_LIVE_TAIL = 40
#: How many `**bold**` / `` `code` `` spans one line may be cut into. A model that
#: writes `a`b`c`d`... sixty times would otherwise pay for sixty tags.
PRISM_SPANS = 4

PRISM_MARK = {"rail": "▌", "crest": "█", "rule": "─", "full": "#"}
PRISM_MARK_PLAIN = {"rail": "|", "crest": "|", "rule": "-", "full": "#"}
PRISM_ROTOR = ("|", "/", "-", "\\")

WORDS = {
    "idle": ("idle", "в покое"),
    "thinking": ("thinking", "думаю"),
    "writing": ("writing", "пишу"),
    "tool": ("running", "выполняю"),
    "denied": ("refused", "отказ"),
    "failed": ("failed", "ошибка"),
    "ran": ("answered", "ответил"),
}

# ------------------------------------------------------------------- small maths --

def prism_float(value) -> float:
    """A number a frame can use, or zero. A NaN clock is not the skin's fault."""
    try:
        step = float(value)
    except (TypeError, ValueError):
        return 0.0
    if step != step or step < 0.0:
        return 0.0
    return step


def prism_int(value, fallback: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def prism_band(index: int, total: int, bands: int, offset: int = 0) -> int:
    """One of `bands` equal slices of the block, walked once around the ramp."""
    step = index * bands // max(1, total)
    return (step * PRISM_LEVEL_STEP + offset) % PRISM_RAMP_LEN


def prism_step_from(seconds: float) -> int:
    """Where the shared border cycle is at `seconds`, as an integer index."""
    return (int(R.phase(seconds, PRISM_FRAME_SECONDS) * PRISM_RAMP_LEN)
            % PRISM_RAMP_LEN)


def prism_step_at(seconds) -> int:
    return prism_step_from(prism_float(seconds))


# ------------------------------------------------------------------ drawing text --

def prism_bracket(text: str) -> str:
    """The one character Rich would read as a tag, defused."""
    return text.replace("[", PRISM_BRACKET)


def prism_hold(text: str, style: str) -> str:
    """Cut every `:word:` shape across a tag boundary, changing nothing else.

    The host renders a markup answer with `Text.from_markup`, which replaces a
    known emoji short code with the emoji — a silent deletion of the model's
    characters. Two chunks in one style look identical and keep the bytes.
    """
    if ":" not in text or not style:
        return text
    gap = PRISM_CLOSE + "[" + style + "]"
    length = len(text)
    parts = []
    index = 0
    while index < length:
        start = text.find(":", index)
        if start < 0:
            parts.append(text[index:])
            break
        walk = start + 1
        closing = False
        while walk < length and walk - start <= 32:
            marker = text[walk]
            if marker == ":":
                closing = walk > start + 1
                break
            if marker.isspace():
                break
            walk += 1
        parts.append(text[index:start + 1])
        if closing:
            parts.append(gap)
        index = start + 1
    return "".join(parts)


def prism_paint(text: str, style: str = "") -> str:
    """`text` wearing `style`, with both ways Rich eats characters defused."""
    if not text:
        return ""
    body = prism_bracket(text)
    if not style:
        return body
    return "[" + style + "]" + prism_hold(body, style) + PRISM_CLOSE


def prism_ink(style: str) -> str:
    """The style this terminal will actually get: none, when it has no colour.

    A skin that cannot paint should not emit paint instructions: with this, the
    colourless answer block and the colourless status line come out as zero tags,
    which is the strongest form of "it still reads as text".
    """
    return style if PRISM.colourful else ""


# ----------------------------------------------------------------------- the logo --

def prism_pixel_word(word: str = PRISM_WORD, scale: int = PRISM_LOGO_SCALE) -> list:
    """The word in the shipped 5x5 shapes, stretched by `scale`."""
    rows = []
    for row_index in range(PRISM_LOGO_ROWS):
        cells = []
        for letter in str(word).upper():
            glyph = PRISM_PIXELS.get(letter, PRISM_PIXELS[" "])
            cells.append("".join(ch * scale for ch in glyph[row_index]))
        rows.append(" ".join(cells))
    return rows


def prism_vectors(rows: list) -> tuple:
    """The art padded to one width, and its columns read across every row."""
    width = max(len(row) for row in rows)
    padded = [row + " " * (width - len(row)) for row in rows]
    columns = [tuple(row[column] for row in padded) for column in range(width)]
    return padded, columns


def prism_squeeze(rows: list, mode: str) -> list:
    """Fewer columns, same letters.

    `tight` drops the columns that hold no ink anywhere — the gutters between the
    letters. `halve` keeps every other column of each run the stretched font
    duplicated, which is exactly the unscaled 5-wide glyph back again. Neither one
    touches a shape, which is why the word is still BEECODE at the narrow end, only
    set tighter.
    """
    padded, columns = prism_vectors(rows)
    plan = [True] * len(columns)
    if mode == "tight":
        blank = (" ",) * len(padded)
        plan = [vector != blank for vector in columns]
    else:
        start = 0
        count = len(columns)
        while start < count:
            stop = start + 1
            while stop < count and columns[stop] == columns[start]:
                stop += 1
            for offset in range(1, stop - start, 2):
                plan[start + offset] = False
            start = stop
    return ["".join(ch for column, ch in enumerate(row) if plan[column])
            for row in padded]


def prism_wider(rows: list, want: int) -> bool:
    return max(len(row) for row in rows) > want


def prism_fit(rows: list, want: int) -> list:
    """The widest of the squeezed forms that still fits `want` columns.

    The ladder goes down by what it costs the reader: the gutters between the
    letters first (76 columns to 70), then the stretch folded out of the letters
    themselves (41), then both at once (35). Below 35 the word cannot be narrowed
    without redrawing the 5x5 font, so this hands the host its own logo at its own
    width and lets the terminal clip it — a cut-off E is honest, a smeared one is
    not, and a word squeezed to fit a 20-column box is just noise.
    """
    if want <= 0 or not prism_wider(rows, want):
        return rows
    gutters = prism_squeeze(rows, "tight")
    if not prism_wider(gutters, want):
        return gutters
    folded = prism_squeeze(rows, "halve")
    if not prism_wider(folded, want):
        return folded
    squeezed = prism_squeeze(folded, "tight")
    if not prism_wider(squeezed, want):
        return squeezed
    # Below the last rung the word cannot be narrowed without redrawing the 5x5
    # font, so the promise here is the one in the docstring: cut it off. Rich
    # *folds* a line that is too wide, which would turn a 30-column terminal into
    # three rows of mixed pixels — a cut-off E is honest, a folded one is noise.
    return [row[:want] for row in squeezed]


def prism_logo_rows(default, want_rows: int, want_cols: int) -> list:
    """The shipped logo as characters, at a width the box can hold.

    `default` is BeeCode's own `Text`, and its `.plain` is the art: taking the
    shape from it is what stops this skin from redrawing somebody else's logo. A
    plain string from an older host reads the same way, and nothing at all falls
    back to this pack's copy of the same 5x5 font.
    """
    body = ""
    try:
        body = str(default.plain)
    except Exception:
        try:
            body = str(default or "")
        except Exception:
            body = ""
    rows = [line.rstrip("\r") for line in body.split("\n")]
    while rows and not rows[-1].strip():
        rows.pop()
    while rows and not rows[0].strip():
        rows.pop(0)
    if not rows:
        rows = prism_pixel_word()
    if want_rows > 0:
        rows = rows[:want_rows]
    if not rows:
        rows = prism_pixel_word()
    return prism_fit(rows, want_cols)


def prism_lit_row(row: str, tilt: float, wave: float, drift: int) -> str:
    """One row of the logo, as the runs of colour that make it up.

    The columns a single level covers leave as one tag, which is what keeps a
    380-cell logo down to some thirty tags per frame — see the cost note above.
    """
    span = max(1, len(row) - 1)
    cells = []
    for column, glyph in enumerate(row):
        if glyph == " ":
            cells.append(PRISM_BLANK_CELL)
            continue
        turn = (column * PRISM_WAVE_WAVES / float(span) + tilt - wave) % 1.0
        if turn < PRISM_FLASH:
            cells.append(PRISM_HOT_CELL)
            continue
        cells.append((int(turn * PRISM_LEVELS) * PRISM_LEVEL_STEP + drift)
                     % PRISM_RAMP_LEN)
    parts = []
    start = 0
    count = len(cells)
    while start < count:
        index = cells[start]
        stop = start + 1
        while stop < count and cells[stop] == index:
            stop += 1
        run = prism_bracket(row[start:stop])
        tag = PRISM_CELL_TAGS[index]
        parts.append(tag + run + PRISM_CLOSE if tag else run)
        start = stop
    return "".join(parts)


def prism_shadow(art: list) -> str:
    """Which columns of the logo hold ink — the word's own footprint, one row."""
    width = max(len(row) for row in art)
    marks = PRISM_MARK["rule"]
    cells = []
    for column in range(width):
        inked = False
        for row in art:
            if column < len(row) and row[column] != " ":
                inked = True
                break
        cells.append(marks if inked else " ")
    return "".join(cells)


def prism_banner(seconds, width: int, rows: int, default) -> str:
    """The logo, lit: a crest across the letters under a palette that rotates."""
    art = prism_logo_rows(default, rows, width)
    clock = prism_float(seconds)
    wave = R.phase(clock, PRISM_WAVE_SECONDS)
    drift = prism_int(R.phase(clock, PRISM_DRIFT_SECONDS) * PRISM_RAMP_LEN)
    lines = [prism_lit_row(row, row_index * PRISM_ROW_TILT, wave, drift)
             for row_index, row in enumerate(art)]
    if rows > len(lines):
        lines.append(prism_lit_row(prism_shadow(art), 0.0, wave, drift))
        lines.extend([""] * (rows - len(lines)))
    return "\n".join(lines)


# --------------------------------------------------------------------- the answer --

def prism_marker(line: str) -> tuple:
    """(kind, head, body): the syntax a line opens with, and what follows it.

    `head` comes back exactly as the model typed it — this pack colours it, it does
    not remove it — so `head + body` is the line, character for character.
    """
    stripped = line.lstrip()
    if not stripped:
        return ("", "", "")
    indent = line[:len(line) - len(stripped)]
    if stripped[:3] in ("```", "~~~"):
        return ("fence", line, "")
    if stripped[:1] == "#":
        hashes = len(stripped) - len(stripped.lstrip("#"))
        if stripped[hashes:hashes + 1] in (" ", "\t"):
            return ("head", indent + stripped[:hashes + 1], stripped[hashes + 1:])
        return ("", line, "")
    if stripped[:1] in ("-", "*", "+") and stripped[1:2] == " ":
        return ("bullet", indent + stripped[0] + " ", stripped[2:])
    if stripped[:1] == ">" and stripped[1:2] == " ":
        return ("quote", indent + "> ", stripped[2:])
    walk = 0
    while walk < len(stripped) and stripped[walk] in "0123456789":
        walk += 1
    if walk > 0 and stripped[walk:walk + 2] in (". ", ") "):
        return ("bullet", indent + stripped[:walk + 2], stripped[walk + 2:])
    return ("", line, "")


def prism_inline(text: str, soft: str, code: str) -> str:
    """Prose with `**strong**` and `` `code` `` picked out; the markers stay put.

    Linear, bounded by `PRISM_SPANS` cuts, and it never moves a character: the
    markers are painted along with what they wrap, so an answer that is still
    arriving cannot lose a `**` to a half-parsed pair.
    """
    parts = []
    index = 0
    spans = 0
    while spans < PRISM_SPANS:
        star = text.find("**", index)
        tick = text.find("`", index)
        if star < 0 and tick < 0:
            break
        take_star = tick < 0 or (0 <= star < tick)
        marker = "**" if take_star else "`"
        start = star if take_star else tick
        closing = text.find(marker, start + len(marker)) if start >= 0 else -1
        if start < 0 or closing < 0:
            break
        style = (PRISM_BOLD + soft) if take_star else code
        parts.append(prism_paint(text[index:start], soft))
        parts.append(prism_paint(text[start:closing + len(marker)], style))
        index = closing + len(marker)
        spans += 1
    parts.append(prism_paint(text[index:], soft))
    return "".join(parts)


def prism_heading(head: str, rest: str, step: int, colourful: bool) -> str:
    """A heading: the rail and the hashes go dim, the letters carry the spectrum."""
    if not colourful:
        return prism_bracket(head + rest)
    levels = max(1, len(rest) // PRISM_LEVELS)
    parts = [prism_paint(head, PRISM_INK)]
    position = 0
    while position < len(rest):
        stop = min(len(rest), position + levels)
        spot = (position * PRISM_LEVEL_STEP * 2 + step + 8) % PRISM_RAMP_LEN
        parts.append(prism_paint(rest[position:stop], PRISM_BOLD + PRISM_RAMP[spot]))
        position = stop
    return "".join(parts)


def prism_window(lines: list, live: bool) -> tuple:
    """The lines this call draws, and whether the first of them is inside a fence.

    Cutting the head off a long live answer would lose the fence parity — the `- `
    that sits inside somebody's code is a bullet to a skin that forgot where the
    block began — so the dropped part is counted, not discarded.
    """
    if not live or len(lines) <= PRISM_LIVE_TAIL:
        return lines, False
    kept = lines[-PRISM_LIVE_TAIL:]
    head = "\n".join(lines[:len(lines) - len(kept)])
    return kept, bool(head.count("```") + head.count("~~~")) % 2


def prism_add(entries, style: str, text: str) -> None:
    """One more line, mergeable with the line above it when the style matches."""
    entries.append((style, text))


def prism_raw(entries, markup: str) -> None:
    """One more line that carries its own tags, so it cannot be merged."""
    entries.append((None, markup))


def prism_join(entries, colourful: bool) -> str:
    """The block, with every run of same-styled lines collapsed into ONE tag.

    This is where the answer's cost is decided: a hundred tinted lines are a
    hundred tags if each is wrapped on its own and about ten if consecutive lines
    that share a colour are joined first. Rich costs about as much per tag as it
    does per kilobyte, so merging is the whole optimisation.
    """
    out = []
    index = 0
    count = len(entries)
    while index < count:
        style, text = entries[index]
        if style is None:
            out.append(text)
            index += 1
            continue
        if not colourful or not style:
            out.append(prism_bracket(text) if not colourful else text)
            index += 1
            continue
        stop = index + 1
        while stop < count and entries[stop][0] == style:
            stop += 1
        block = prism_bracket("\n".join(entry[1] for entry in entries[index:stop]))
        out.append("[" + style + "]" + prism_hold(block, style) + PRISM_CLOSE)
        index = stop
    return "\n".join(out)


def prism_answer(body: str, tick: int, colourful: bool, live: bool) -> str:
    """The answer as markup. Every character of it is the model's."""
    lines = body.split("\n")
    draw, in_fence = prism_window(lines, live)
    total = max(1, len(draw))
    step = tick % PRISM_RAMP_LEN
    crest = int((tick % PRISM_TRAVEL_STEPS) * total / float(PRISM_TRAVEL_STEPS))
    # A three-line band of light on a block long enough to show one; on a two-line
    # answer the whole block would be lit at once, which is a strobe, not a sweep.
    lit = 1 if total >= 12 else 0
    marks = PRISM_MARK if colourful else PRISM_MARK_PLAIN
    entries = []
    for index, line in enumerate(draw):
        tint = PRISM_SOFT[prism_band(index, total, PRISM_BANDS, step)]
        if not line.strip():
            prism_add(entries, tint, "")
            continue
        kind, head, rest = prism_marker(line)
        if kind == "fence":
            in_fence = not in_fence
            prism_raw(entries, prism_paint(marks["rail"] + " " + line,
                                           prism_ink(PRISM_BOLD + PRISM_INK)))
            continue
        if in_fence:
            # Code is left alone: no marker, no emphasis, no substitution. The
            # blank margin is the only addition, and the characters stay the
            # model's in the order the model put them.
            prism_add(entries, PRISM_CODE, " " * PRISM_GUTTER + line)
            continue
        if abs(index - crest) <= lit:
            # The crest of the band of light that walks down the block. One style
            # for the three lit lines, so they merge with each other and break the
            # runs around them, which is exactly what a moving highlight looks like.
            prism_add(entries, PRISM_BOLD + PRISM_GLOW[step],
                      marks["crest"] + " " + line)
            continue
        if kind == "head":
            # The rail rides inside the dim hash paint, so the heading keeps the
            # block's left edge without costing a tag of its own.
            prism_raw(entries, prism_heading(marks["rail"] + " " + head, rest,
                                             step, colourful))
            continue
        if kind == "quote":
            prism_add(entries, "italic " + tint, marks["rail"] + " " + line)
            continue
        if "**" in line or "`" in line:
            # The rail rides in front of the line inside the same first fragment,
            # so painting the emphasis costs no tag for the margin.
            prism_raw(entries, prism_inline(marks["rail"] + " " + line, tint,
                                            PRISM_CODE))
            continue
        # Prose, a bullet (the dash is the model's own character, painted with its
        # line) and a table row all land in the tinted run they belong to.
        prism_add(entries, tint, marks["rail"] + " " + line)
    return prism_join(entries, colourful)


# ------------------------------------------------------------------------ state ---

@dataclasses.dataclass
class PrismState:
    """What the skin remembers: one instance per switch, nothing per call."""

    lang: str = "en"
    talk: object = None
    state: str = "idle"
    tool: str = ""
    pieces: int = 0
    tokens: int = 0
    lines: int = 0
    seconds: float = 0.0
    tick: int = 0
    colourful: bool = True
    last_text: str = ""
    last_tick: int = -1
    last_colour: bool = True
    last_markup: str = ""

    def reset(self) -> None:
        """A fresh turn: the strip and the state line describe *this* request."""
        self.state = "idle"
        self.tool = ""
        self.pieces = 0
        self.tokens = 0
        self.lines = 0
        self.forget()

    def forget(self) -> None:
        """Drop the one cached answer. The text is the key, so it is the whole key."""
        self.last_text = ""
        self.last_tick = -1
        self.last_colour = True
        self.last_markup = ""

    def say(self, english: str, russian: str) -> str:
        """Words in the user's language.

        `beeagent.i18n` is off the import allow-list for a skin, so the translator
        arrives on the context when the host has one to hand out; until then this is
        the same two-way rule with the same default.
        """
        talk = self.talk
        if talk is not None:
            try:
                return str(talk(english, russian))
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    def word(self) -> str:
        return self.say(*WORDS.get(self.state, WORDS["idle"]))

    def counts(self) -> str:
        """Tokens this turn, and their rate once there is a turn to divide by."""
        if not self.tokens:
            return ""
        if self.seconds <= 0.5:
            return "%dt" % self.tokens
        return "%dt %.0ft/s" % (self.tokens, self.tokens / self.seconds)

    def marks(self) -> dict:
        return PRISM_MARK if self.colourful else PRISM_MARK_PLAIN

    def advance(self, dt) -> None:
        """The clock the host is already keeping, turned into this pack's phase."""
        self.seconds += prism_float(dt)
        self.tick = int(self.seconds * PRISM_RAMP_LEN / PRISM_TRAVEL_SECONDS)

    def on_event(self, event: str, payload) -> None:
        data = payload if isinstance(payload, dict) else {}
        if event == "reasoning_delta":
            self.state = "thinking"
        elif event == "stream_delta":
            self.state = "writing"
            self.pieces += 1
            self.tokens += 1
        elif event == "tool_start":
            self.state = "tool"
            self.tool = prism_clean(data.get("tool", "?"), 24)
        elif event == "tool_end":
            self.state = "failed" if data.get("error") else "ran"
            self.tool = ""
        elif event == "tool_denied":
            self.state = "denied"
            self.tool = prism_clean(data.get("tool", "?"), 24)
        elif event == "retry":
            self.tool = prism_clean(data.get("reason", "") or "retry", 24)
        elif event == "error":
            self.state = "failed"
        elif event == "done":
            self.state = "ran"
            self.tool = ""
        elif event == "stopped":
            self.reset()


def prism_clean(text, limit: int = 32) -> str:
    """Event payload as screen ink: printable, one line, bounded.

    Tool names and arguments are text a model wrote. A payload carrying an escape
    sequence must not be able to move the cursor from inside a label.
    """
    kept = [ch for ch in str(text) if 32 <= ord(ch) != 127][:limit]
    return "".join(kept).strip()


PRISM = PrismState()

# ------------------------------------------------------------ one-line surfaces --

def prism_status(default: str) -> str:
    """The state line: our word, the tool, the numbers, then the host's own text.

    ASCII glyphs throughout, and the host's wording is kept rather than replaced: a
    Russian interface stays Russian on this line, only the colour is ours.
    """
    tick = PRISM.tick % PRISM_RAMP_LEN
    parts = [prism_paint(PRISM.word(),
                         prism_ink(PRISM_BOLD + PRISM_RAMP[tick % PRISM_RAMP_LEN]))]
    if PRISM.tool:
        parts.append(prism_paint(PRISM.tool,
                                 prism_ink(PRISM_RAMP[(tick + 11) % PRISM_RAMP_LEN])))
    numbers = PRISM.counts()
    if numbers:
        parts.append(prism_paint(numbers, prism_ink(PRISM_INK)))
    if default:
        parts.append(prism_paint(default, prism_ink(PRISM_INK)))
    return " - ".join(parts)


def prism_spinner(default: str, clock) -> str:
    """The waiting line, phased by the monotonic clock the host is already keeping."""
    if not default:
        return None
    turn = R.phase(prism_float(clock), PRISM_SPINNER_SECONDS)
    place = int(turn * 4) % 4
    filled = int(turn * 6)
    bar = "[" + "#" * filled + "." * (6 - filled) + "] " + PRISM_ROTOR[place]
    index = min(PRISM_RAMP_LEN - 1, int(turn * PRISM_RAMP_LEN))
    return (prism_paint(default, prism_ink(PRISM_BOLD + PRISM_SOFT[index])) + " " +
            prism_paint(bar, prism_ink(PRISM_RAMP[index])))


def prism_thinking(line: str) -> str:
    """Plain text, not markup: the host prints these lines inside its own `Text`.

    The rail is the answer's rail, two cells wide, so a block of reasoning and the
    reply that follows it read as one column. No colour is possible here, and no
    non-ASCII glyph either: this line reaches terminals that cannot draw one.
    """
    if not line:
        return line
    return "| " + line


# ---------------------------------------------------------------------- the border --

def prism_frame_style(role, seconds):
    """One panel's border colour: the shared cycle, at that role's own point."""
    if not PRISM.colourful:
        return None                 # nothing to animate with: keep BeeCode's frame
    word = prism_clean(role, 16).lower()
    step = prism_step_at(seconds)
    if word in PRISM_ERROR_ROLES:
        # Red, and only red. The shade walks, so an error panel is still visibly
        # alive next to the others, but it never leaves the red family.
        return PRISM_BOLD + PRISM_ERROR[(step + 4) % PRISM_ERROR_LEN]
    return PRISM_BOLD + PRISM_RAMP[(step + PRISM_SHIFT.get(word, 0)) % PRISM_RAMP_LEN]


# --------------------------------------------------------------------------- the hud --

def prism_size(painter) -> tuple:
    try:
        cols, rows = painter.get_terminal_size()
        return max(1, prism_int(cols, 80)), max(1, prism_int(rows, HUD_ROWS))
    except Exception:
        return 80, max(1, HUD_ROWS)


def prism_draw(painter, widget: str, *args, **kwargs) -> bool:
    """`painter.<widget>()`, when this build of the painter has one.

    The six documented methods are a floor and the bars are a later addition; a pack
    that calls a newer widget blind raises on the host it was not written against,
    and the engine reports that as "this skin lost the hud surface" rather than as
    "old host". False means the caller has the fallback to hand.
    """
    try:
        if widget == "text":
            painter.draw_text(*args, **kwargs)
        elif widget == "clear":
            painter.clear_region(*args, **kwargs)
        elif widget == "bar":
            painter.draw_bar(*args, **kwargs)
        elif widget == "ramp":
            painter.draw_ramp(*args, **kwargs)
        elif widget == "ticker":
            painter.draw_ticker(*args, **kwargs)
        else:
            return False
    except Exception:
        return False
    return True


def prism_runs(painter, x: int, y: int, width: int, drift: int,
               glyph: str = "#", tint: bool = False) -> None:
    """The spectrum across a row, as `PRISM_RUNS` long runs instead of one call per
    cell.

    `draw_ramp` is the widget for this, and it costs eight times what the same
    pixels cost here: it blends and blits every cell, and a cell is one more
    `draw_text` call the renderer has to warn, clip and intern. Bands of a dozen
    identical colours are indistinguishable at terminal size anyway.
    """
    if width <= 0:
        return
    band = max(1, width // PRISM_RUNS)
    table = PRISM_SOFT if tint else PRISM_RAMP
    start = 0
    while start < width:
        span = min(band, width - start)
        index = prism_band(start, width, PRISM_RUNS, drift)
        prism_draw(painter, "text", x + start, y, glyph * span,
                   color=table[index % PRISM_RAMP_LEN])
        start += span


def prism_bar(painter, x: int, y: int, width: int, part: float, colour: str) -> None:
    """A saturating bar: it says "a lot has arrived", not "almost done".

    Nothing here knows how long the answer will be, and a decoration that implies it
    does is the oldest lie in a progress indicator.
    """
    filled = int(round(width * min(1.0, max(0.0, part))))
    marks = PRISM_MARK["full"]
    if filled:
        prism_draw(painter, "text", x, y, marks * filled, color=colour, style="bold")
    if width > filled:
        prism_draw(painter, "text", x + filled, y, "." * (width - filled),
                   color=PRISM_INK)


def prism_hud(painter, dt) -> None:
    """The reserved strip: state and numbers on top, spectrum and tool below."""
    cols, rows = prism_size(painter)
    if cols < PRISM_MIN_HUD_COLS:
        return
    if not prism_draw(painter, "clear", 0, 0, cols, min(rows, HUD_ROWS)):
        return
    tick = PRISM.tick % PRISM_RAMP_LEN
    word = " " + prism_clean(PRISM.word(), 20) + " "
    numbers = prism_clean(PRISM.counts(), 20)
    elapsed = "%ds" % int(PRISM.seconds) if PRISM.seconds >= 1 else ""

    prism_draw(painter, "text", 0, 0, word, color=PRISM_RAMP[tick], bg=PRISM_DEEP,
               style="bold")
    edge = cols - len(elapsed) if elapsed else cols
    if elapsed:
        prism_draw(painter, "text", max(0, cols - len(elapsed)), 0, elapsed,
                   color=PRISM_INK)
    right = edge - len(numbers) - 1 if numbers else edge
    if numbers:
        prism_draw(painter, "text", max(0, right), 0, numbers, color=PRISM_SOFT[tick])
    bar_x = len(word) + 1
    bar_room = right - bar_x - 1
    if bar_room >= 6:
        prism_bar(painter, bar_x, 0, min(28, bar_room),
                  PRISM.tokens / float(PRISM.tokens + 60), PRISM_RAMP[tick])
    if rows < 2:
        return

    drift = prism_int(R.phase(PRISM.seconds, PRISM_DRIFT_SECONDS) * PRISM_RAMP_LEN)
    colour = PRISM_RAMP[(drift + 8) % PRISM_RAMP_LEN]
    label = prism_clean(PRISM.tool, 24) or PRISM.say("no tool", "нет инструмента")
    # A fixed name field, then the strip: stable geometry is what lets a person read
    # the same row twice without hunting for where the number went.
    name_w = min(max(8, cols // 2), edge - 1)
    scrolled = False
    if R.text_cells(label) > name_w:
        # A name that does not fit the field scrolls rather than being cut in half
        # when this painter knows the widget: a truncated `bas` is no tool anybody
        # can act on.
        scrolled = prism_draw(painter, "ticker", 0, 1, name_w, label,
                              phase=R.phase(PRISM.seconds, 6.0), color=colour)
    if not scrolled:
        prism_draw(painter, "text", 0, 1, label[:name_w], color=colour, style="bold")
    strip_x = name_w + 1
    span = edge - strip_x
    if span >= 6:
        prism_runs(painter, strip_x, 1, span, drift)
        crest = int(R.phase(PRISM.seconds, PRISM_TRAVEL_SECONDS) * span)
        prism_draw(painter, "text", min(edge - 1, strip_x + crest), 1,
                   PRISM_MARK["crest"], color=PRISM_HOT, style="bold")


# ------------------------------------------------------------- the hook bodies ----

def prism_answer_of(text, final: bool):
    """The block, with one cheap guard: the same answer, the same phase."""
    body = str(text or "")
    if not body.strip():
        return None
    tick = PRISM.tick
    colourful = PRISM.colourful
    if (body == PRISM.last_text and tick == PRISM.last_tick
            and colourful == PRISM.last_colour and PRISM.last_markup):
        return PRISM.last_markup          # the same phase asks for the same block
    markup = prism_answer(body, tick, colourful, not final)
    PRISM.last_text = body
    PRISM.last_tick = tick
    PRISM.last_colour = colourful
    PRISM.last_markup = markup
    return markup


# ----------------------------------------------------------------------- the hooks ---

def on_init(ctx) -> None:
    PRISM.reset()
    PRISM.seconds = 0.0
    PRISM.tick = 0
    try:
        PRISM.lang = prism_clean(ctx.language(), 8) or "en"
    except Exception:
        PRISM.lang = "en"
    try:
        talk = ctx.translate
        PRISM.talk = talk if callable(talk) else None
    except Exception:
        PRISM.talk = None
    colourful = True
    try:
        colourful = str(R.color_support() or "") not in ("", "none")
    except Exception:
        colourful = True
    PRISM.colourful = colourful


def on_surfaces(ctx):
    """Ask for what this screen can hold; a refused claim costs nothing."""
    try:
        cols, screen_rows = ctx.size()
    except Exception:
        cols, screen_rows = 80, 24
    wanted = ["banner", "frame", "answer", "status", "spinner", "thinking"]
    if int(cols or 0) >= PRISM_MIN_HUD_COLS and int(screen_rows or 0) >= 8:
        wanted.append("hud")
    return wanted


def on_event(event, payload) -> None:
    PRISM.on_event(event, payload)


def on_output(text) -> None:
    if text:
        PRISM.lines += 1


def on_frame(dt=0.0, painter=None) -> None:
    PRISM.advance(dt)


def on_banner(seconds, width=0, rows=0, default=None):
    """The logo, once per animation frame: markup of `rows` lines, or nothing."""
    if not PRISM.colourful:
        # With the colours taken away, a colour cycle is the same five rows of
        # blocks the shipped logo already draws, so let the shipped logo draw them.
        return None
    return prism_banner(seconds, prism_int(width), prism_int(rows), default)


def on_frame_color(role="", seconds=0.0):
    """The border colour for one panel role, or None to keep BeeCode's."""
    return prism_frame_style(role, seconds)


def on_status(default):
    return prism_status(default or "")


def on_spinner(default, clock=0.0):
    return prism_spinner(default or "", clock)


def on_thinking(line):
    return prism_thinking(line or "")


def on_answer(text, final=False):
    return prism_answer_of(text, bool(final))


def on_hud(painter, dt=0.0):
    prism_hud(painter, dt)


def hooks() -> dict:
    """This pack's lifecycle, by name — what `setup` hands to the host."""
    return {"SURFACES": SURFACES, "HUD_ROWS": HUD_ROWS, "NAME": NAME,
            "on_init": on_init, "on_surfaces": on_surfaces, "on_event": on_event,
            "on_output": on_output, "on_frame": on_frame, "on_banner": on_banner,
            "on_frame_color": on_frame_color, "on_status": on_status,
            "on_spinner": on_spinner, "on_thinking": on_thinking,
            "on_answer": on_answer, "on_hud": on_hud}


# ------------------------------------------------------------------ registration ---

def setup(api) -> None:
    """Hand the skin to the host; register listeners only if it will not take it.

    Nothing here reaches into `beeagent.ui`: the logo, the border colour and the
    answer are *surfaces* now, asked about through `beeagent.core.skins`, so the
    legacy slots stay the user's and `/skin frame none` still means no box.
    """
    if not attach(api):
        for name in ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
                     "tool_denied", "done", "stopped", "error"):
            api.event(name, on_event)
    contribute(api)


def attach(api) -> bool:
    """The lifecycle, through whichever verb this build of the API answers to.

    No `getattr` anywhere in this pack, and that is not an accident: the gate
    refuses a skin that reaches for it, because a computed attribute name is how
    code walks out from under a syntax check. Trying the call and catching what
    comes back asks the same question without the escape route.
    """
    given = hooks()
    try:
        api.skin_hooks(NAME, given, DESCRIPTION)
        return True
    except Exception:
        pass
    try:
        api.skin_hooks(NAME, given)
        return True
    except Exception:
        pass
    try:
        return not S.register(NAME, given, description=DESCRIPTION, pack=PACK).refused
    except Exception:
        return False


def contribute(api) -> None:
    """Say so in `/extensions`, whichever route took the hooks."""
    try:
        api.skin("skins", NAME, hooks())
    except Exception:
        pass
