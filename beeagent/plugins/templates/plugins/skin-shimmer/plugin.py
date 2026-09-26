"""`skin-shimmer` — the skin that draws the answer, not just the line above it.

What it takes over:

* the **answer block** (the `answer` surface): the model's markdown inside an
  outline whose colour walks the honey→leaf ramp cell by cell along every edge,
  list markers that walk the same ramp item by item, headings painted across it,
  and a rail on each continuation line. Both interfaces show it — the classic REPL
  through `render_response`, the TUI through its log and its live area;
* the **streaming answer** in the classic REPL, through the `stream` slot: a
  `ResponseStream` subclass, so the fence shield, the thinking tail, F2 and the
  whole-line discipline stay BeeCode's and only the ink differs;
* the **panels and tables** of the classic REPL, through the `frame` slot;
* `status`, `spinner`, `thinking` and the `hud` strip, the way `skin-hud` does.

Two rules this pack is written against, because it paints the largest thing on the
screen:

**Motion lives in the content.** This version of Rich gives a `Box` no per-edge
style, so the outline is drawn by hand from laid-out lines — which also means it is
rebuilt on every layout. A panel's border is drawn once and scrolls away; the
answer area is laid out again on every frame. So the moving part goes where it is
repainted (markers, rails, HUD, status lines), and the `frame` slot's border stays
one static sweep of the ramp. Mutating a shared box per frame, from the agent's
thread and the paint thread at once, is the bug that reasoning avoids.

**Nothing is computed per character per frame.** The ramp is 32 colours chosen at
import and indexed after that; a laid-out block is cached against its own text; and
the answer is laid out as markdown only *once it is finished* — while it arrives,
the same lines, bullets and headings are drawn without a parse, because a frame
that re-typesets forty pages twelve times a second is a skin that costs the user
their own typing.
"""
import beeagent.core.renderer as R
from rich.markdown import Heading, ListElement, ListItem, Markdown
from rich.segment import Segment
from rich.text import Text

NAME = "shimmer"
SURFACES = ("status", "spinner", "thinking", "answer", "hud")
HUD_ROWS = 2

HONEY = "#ffcc00"
PALE = "#fff9c4"
LEAF = "#7cb342"
DEEP_LEAF = "#43a047"
DEEP = "#2b2113"
RAIL = "▏"
BULLET = "•"

#: Colours on the walk, computed once at import. Ping-pong — honey to leaf and back
#: — so the index wraps without a seam where one colour stops and the other starts.
STEPS = 16
_RAMP = tuple(R.blend(HONEY, LEAF, R.ease("in_out", index / (STEPS - 1.0)))
              for index in range(STEPS)) + tuple(
              R.blend(LEAF, HONEY, R.ease("in_out", index / (STEPS - 1.0)))
              for index in range(1, STEPS - 1))
CYCLE = len(_RAMP)

#: Laid-out blocks kept, newest last. An answer is read once; a cache that grows
#: with the session is a leak with a friendly name.
CACHE_MAX = 6
#: Narrowest screen worth outlining. Below it the rails would eat the text width.
MIN_FRAME_COLS = 24

STATE = {
    "idle": ("idle", "в покое"),
    "thinking": ("thinking", "думаю"),
    "writing": ("writing", "пишу"),
    "tool": ("running", "выполняю"),
    "denied": ("refused", "отказ"),
    "failed": ("failed", "ошибка"),
    "ran": ("answered", "ответил"),
}


def tone(index: int) -> str:
    """The ramp's colour at `index`, wrapped. One list lookup; no maths per cell."""
    return _RAMP[index % CYCLE]


def across(position: int, count: int, shift: int = 0) -> str:
    """The colour at `position` of `count`, so a whole edge spans the ramp once."""
    span = max(1, count - 1)
    return tone(int(position * (CYCLE - 1) / span) + shift)


def clean(text, limit: int = 32) -> str:
    kept = [ch for ch in str(text) if 32 <= ord(ch) != 127][:limit]
    return "".join(kept).strip()


def size_of(painter) -> tuple:
    ask = getattr(painter, "get_terminal_size", None)
    if not callable(ask):
        return 80, 1
    try:
        cols, rows = ask()
        return max(1, int(cols)), max(1, int(rows))
    except Exception:
        return 80, 1


def call(painter, widget: str, *args, **kwargs) -> bool:
    """`painter.<widget>()` when this build of it has one. False means it does not."""
    run = getattr(painter, widget, None)
    if not callable(run):
        return False
    try:
        run(*args, **kwargs)
    except Exception:
        return False
    return True


# ------------------------------------------------- the answer, laid out ---------

class ShimmerItem(ListItem):
    """A list item whose marker carries the ramp at that item's place in the list."""

    def render_bullet(self, console, options):
        render_options = options.update_width(max(4, options.max_width - 3))
        lines = console.render_lines(self.elements, render_options, style=self.style)
        style = self._marker_style(console)
        bullet = Segment(BULLET + " ", style)
        padding = Segment(" " * 3)
        new_line = Segment("\n")
        first = True
        for line in lines:
            yield bullet if first else padding
            first = False
            yield from line
            yield new_line

    def render_number(self, console, options, number, last_number):
        width = len(str(last_number)) + 2
        render_options = options.update_width(max(4, options.max_width - width))
        lines = console.render_lines(self.elements, render_options, style=self.style)
        numeral = Segment(str(number).rjust(width - 1) + " ", self._marker_style(console))
        padding = Segment(" " * width)
        new_line = Segment("\n")
        first = True
        for line in lines:
            yield numeral if first else padding
            first = False
            yield from line
            yield new_line

    def _marker_style(self, console):
        colour = tone(shimmer.phase + shimmer.next_marker())
        if not shimmer.colourful:
            return console.get_style("none")
        return console.get_style(colour)


class ShimmerList(ListElement):
    """Tells the skin how long this list is, so the ramp spans it once."""

    def __rich_console__(self, console, options):
        shimmer.open_list(len(self.items))
        yield from ListElement.__rich_console__(self, console, options)


class ShimmerHeading(Heading):
    """The heading's own letters carry the ramp: `## parts` reads as a band."""

    def __rich_console__(self, console, options):
        text = self.text.copy()
        text.justify = self.LEVEL_ALIGN.get(self.tag, "left")
        if shimmer.colourful:
            body = text.plain
            for position in range(len(body)):
                text.stylize(across(position, len(body), shimmer.phase),
                             position, position + 1)
        yield text


class Answer(Markdown):
    """Markdown with our three elements swapped in, Rich's for everything else.

    Paragraphs, code, tables and links stay as they are: inline formatting the
    model wrote (`**bold**`, a list inside a list) is exactly what a hand-rolled
    renderer would quietly throw away.
    """

    elements = dict(Markdown.elements)
    elements.update({"bullet_list_open": ShimmerList, "ordered_list_open": ShimmerList,
                     "list_item_open": ShimmerItem, "heading_open": ShimmerHeading})


class Framed:
    """The block inside an outline whose colour walks every edge.

    Drawing the frame by hand from the laid-out lines is the only way to a gradient
    border in this Rich: `Box` styles the whole border at once. The rows come from
    `render_lines`, which is what Rich's own `Panel` does, so the answer inside
    keeps its own layout, padding and colours.
    """

    def __init__(self, inner, title: str = "", phase: int = 0):
        self.inner = inner
        self.title = title
        self.phase = phase

    def __rich_console__(self, console, options):
        width = options.max_width
        if width < MIN_FRAME_COLS:
            yield from console.render(self.inner, options)
            return
        body = width - 4
        rows = console.render_lines(self.inner, options.update_width(body), pad=True)
        label = (" " + self.title + " ") if self.title else ""
        yield self._rule(width, "┌", "┐", "─", label)
        total = max(1, len(rows) + 1)
        for index, row in enumerate(rows):
            yield self._side(row, index, total, body)
        yield self._rule(width, "└", "┘", "─", "")

    def _rule(self, width: int, first: str, last: str, dash: str, label: str) -> Text:
        """One horizontal edge: the ramp across it, with the label set into it."""
        glyphs = [dash] * width
        glyphs[0], glyphs[-1] = first, last
        start = -1
        if label and width > len(label) + 8:
            start = 2 + (width - len(label) - 4) // 2
            for offset in range(len(label)):
                glyphs[start + offset] = " "
        line = Text()
        position = 0
        while position < width:
            colour = across(position, width, self.phase)
            if position == start:
                line.append(label, style="bold " + colour)
                position += len(label)
                continue
            line.append(glyphs[position], style=colour)
            position += 1
        return line

    def _side(self, row, index: int, total: int, body: int) -> Text:
        """One laid-out line of the answer, between two rails of its own colour."""
        colour = across(index, total, self.phase)
        line = Text()
        line.append(RAIL + " ", style=colour if shimmer.colourful else None)
        for segment in row:
            line.append(segment.text, style=segment.style)
        if line.cell_len < body:
            line.append(" " * (body - line.cell_len))
        line.append(" " + RAIL, style=colour if shimmer.colourful else None)
        return line


#: Lines the live streaming area keeps. The answer is read at the bottom edge while
#: it arrives, so laying out the whole of a forty-page answer twelve times a second
#: buys nothing a person can see — and this is what makes the cost of a frame
#: independent of how long the answer has grown.
LIVE_TAIL = 24


def lines_of(text: str, phase: int = 0, colour_it: bool = True,
             tail: int = 0) -> Text:
    """The cheap block: the same lines, railed and marked, without a markdown parse.

    Used while the answer is arriving, and on a terminal that says it has no
    colour, where parsing would cost the frame and buy nothing to show for it.
    `tail` keeps only that many last lines — what a live area can display.
    """
    body = str(text or "")
    lines = body.split("\n")
    if tail and len(lines) > tail:
        lines = lines[-tail:]
    block = Text()
    marker = 0
    fenced = False
    for raw in lines:
        line = raw.rstrip()
        stripped = line.lstrip()
        indent = line[:len(line) - len(stripped)]
        if stripped.startswith("```"):
            fenced = not fenced          # inside a fence, `- ` and `#` are code
        elif fenced:
            pass
        elif stripped[:1] in ("-", "*", "+") and stripped[1:2] == " ":
            if colour_it:
                block.append(indent + BULLET + " ",
                             style=across(marker, 24, phase))
                block.append(stripped[2:] + "\n")
                marker += 1
                continue
        elif stripped.startswith("#") and colour_it:
            head = stripped.lstrip("#").strip()
            for position in range(len(head)):
                block.append(head[position], style=across(position, len(head), phase))
            block.append("\n")
            marker += 1
            continue
        if colour_it:
            block.append(RAIL + " ", style=tone(phase + marker))
        block.append(line + "\n")
        marker += 1
    return block


# ------------------------------------------------------------- the skin ---------

class Shimmer:
    """Phase, counts, and the two caches the answer path needs."""

    __slots__ = ("lang", "translate", "state", "tool", "pieces", "tokens",
                 "seconds", "lines", "phase", "colourful", "_blocks", "_order",
                 "_list_total", "_list_index")

    def __init__(self):
        self.lang = "en"
        self.translate = None
        self.state = "idle"
        self.tool = ""
        self.pieces = 0
        self.tokens = 0
        self.seconds = 0.0
        self.lines = 0
        self.phase = 0
        self.colourful = True
        self._blocks = {}
        self._order = []
        self._list_total = 1
        self._list_index = 0

    def reset(self) -> None:
        self.pieces = 0
        self.tokens = 0
        self.lines = 0
        self.tool = ""
        self.state = "idle"
        self._blocks.clear()
        self._order.clear()

    # -- words ----------------------------------------------------------------

    def say(self, english: str, russian: str) -> str:
        """Two languages, one call; `beeagent.i18n` is off the allow-list.

        `__slots__` costs a line of explanation each: this object exists once per
        install and is asked on every frame, so the dictionary it does not build is
        the point.
        """
        if self.translate is not None:
            try:
                return self.translate(english, russian)
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    def word(self) -> str:
        return self.say(*STATE.get(self.state, STATE["idle"]))

    def counts(self) -> str:
        if not self.tokens:
            return ""
        if self.seconds <= 0.5:
            return "%dt" % self.tokens
        return "%dt %.0f/s" % (self.tokens, self.tokens / self.seconds)

    # -- the clock -------------------------------------------------------------

    def advance(self, dt) -> None:
        try:
            step = float(dt)
        except (TypeError, ValueError):
            step = 0.0
        if step != step or step < 0:
            step = 0.0
        self.seconds += step
        if step:
            # An integer phase, one step a frame: every colour on screen is then an
            # index into a tuple, which is the whole cost of animating them.
            self.phase = (self.phase + 1) % CYCLE

    # -- list markers -----------------------------------------------------------

    def open_list(self, count: int) -> None:
        self._list_total = max(1, int(count))
        self._list_index = 0

    def next_marker(self) -> int:
        """How far along this list the marker is, scaled to span the ramp once."""
        index = min(self._list_index, self._list_total - 1)
        offset = index * (CYCLE - 1) // self._list_total
        self._list_index += 1
        return int(offset)

    # -- the surfaces -------------------------------------------------------------

    def status(self, default: str) -> str:
        line = R.markup(self.word(), HONEY, style="bold")
        if self.tool:
            line += " " + R.markup(self.tool, LEAF)
        numbers = self.counts()
        if numbers:
            line += " " + R.markup(numbers, "grey42")
        return line + ((" · " + default) if default else "")

    def spinner(self, default: str, clock):
        if not default:
            return None
        step = int(R.phase(clock, 1.4) * 8)
        return R.ramp_markup(default, HONEY, LEAF) + " " + "·" * (step % 4)

    def thinking(self, line: str) -> str:
        return ("» " + line) if line else line

    def answer(self, text: str, final: bool = False):
        """The block, cached against its own text; cheap while it is still arriving.

        The full markdown layout is the expensive one — a twenty-line answer costs
        milliseconds to lay out, and the streaming area asks again every frame, so
        the arriving answer gets the line drawing and the finished one gets the
        block. Both carry the rails, the ramp on the bullets and the coloured
        headings; only the parse differs.
        """
        body = str(text or "")
        if not body.strip():
            return None
        if not final:
            return lines_of(body, self.phase, self.colourful, LIVE_TAIL)
        cached = self._blocks.get(body)
        if cached is not None:
            return cached
        try:
            block = Framed(Answer(body), title=self.say("BeeCode", "пчела"),
                           phase=self.phase if self.colourful else 0)
        except Exception:
            block = lines_of(body, self.phase, self.colourful)
        self._remember(body, block)
        return block

    def _remember(self, text: str, block) -> None:
        self._blocks[text] = block
        self._order.append(text)
        while len(self._order) > CACHE_MAX:
            oldest = self._order.pop(0)
            if oldest != text:
                self._blocks.pop(oldest, None)

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
            self.tool = clean(data.get("tool", "?"), 24)
        elif event == "tool_end":
            self.state = "failed" if data.get("error") else "ran"
            self.tool = ""
        elif event == "tool_denied":
            self.state = "denied"
            self.tool = clean(data.get("tool", "?"), 24)
        elif event == "error":
            self.state = "failed"
        elif event == "done":
            self.state = "ran"
            self.tool = ""
        elif event == "stopped":
            self.reset()

    # -- the strip ------------------------------------------------------------------

    def hud(self, painter, dt: float) -> None:
        cols, rows = size_of(painter)
        if cols < MIN_FRAME_COLS * 2:
            return
        if not call(painter, "clear_region", 0, 0, cols, rows):
            return
        label = " " + self.word() + " "
        call(painter, "draw_text", 0, 0, label, color=HONEY, bg=DEEP, style="bold")
        numbers = self.counts()
        if numbers:
            call(painter, "draw_text", max(0, cols - len(numbers)), 0, numbers,
                 color=LEAF)
        bar_x = len(label) + 1
        room = max(0, cols - bar_x - len(numbers) - 2)
        if room >= 6:
            # A saturating bar: it says "a lot has arrived", not "almost done",
            # because nothing here knows how long the answer will be.
            part = self.tokens / float(self.tokens + 40)
            if not call(painter, "draw_bar", bar_x, 0, min(24, room), part,
                        color=HONEY, filled="#", empty="."):
                self._fallback_bar(painter, bar_x, min(24, room), part)
        if rows < 2:
            return
        self._hud_second(painter, cols, rows)

    def _hud_second(self, painter, cols: int, rows: int) -> None:
        """The second row: what is running now, and for how long.

        A repeating marquee of a nine-character tool name reads as noise, so the
        name is written once and only scrolled when the row cannot hold it.
        """
        row = 1
        elapsed = "%ds" % int(self.seconds) if self.seconds >= 1 else ""
        if elapsed:
            call(painter, "draw_text", max(0, cols - len(elapsed)), row, elapsed,
                 color=DEEP_LEAF)
        room = max(0, cols - len(elapsed) - 1)
        text = self.tool or self.say("no tool", "тулз не запущен")
        if room < 4:
            return
        if R.text_cells(text) <= room or not call(painter, "draw_ticker", 0, row, room,
                                                  text, phase=R.phase(self.seconds, 6.0),
                                                  color=PALE, gap=max(4, room // 2)):
            call(painter, "draw_text", 0, row, text[:room], color=PALE)
        if rows > 2 and self.lines:
            tail = "%d %s" % (self.lines, self.say("lines", "строк"))
            call(painter, "draw_text", 0, 2, tail[:cols], color=LEAF)

    def _fallback_bar(self, painter, x: int, width: int, part: float) -> None:
        """The bar as one text run, for a painter with no `draw_bar`."""
        filled = int(round(width * min(1.0, max(0.0, part))))
        call(painter, "draw_text", x, 0, "#" * filled + "." * (width - filled),
             color=HONEY)


shimmer = Shimmer()


# --- the streaming printer, classic REPL only ---------------------------------------

_RailStream = None


def rail_stream():
    """The built-in answer printer with a railed line discipline of our own.

    Imported lazily and built once: `beeagent.ui.components` is a heavy module, and
    a pack that reached for it at import time would slow down every start of
    BeeCode, not only the ones wearing this skin.
    """
    global _RailStream
    if _RailStream is not None:
        return _RailStream
    from beeagent.ui import components

    class RailStream(components.ResponseStream):
        """Same buffers, same fences, same thinking tail — different ink.

        Only the two writes are overridden, so every rule the host learned the hard
        way — whole lines only while the prompt owns the screen, never print a
        tool-call payload, end the turn on a line boundary — keeps applying here.
        """

        def _emit(self, line: str) -> None:
            """One finished line of the arriving answer, railed and marked.

            The text is the model's bytes and stays them: only a list marker and a
            heading's colour are ours. A fenced block is never restyled, and this
            tracks the fence itself — the host's `_in_fence` is already false by the
            time a closed block reaches `_print`, so reading it here would colour
            the `- ` that sits inside somebody's code.
            """
            self._rail_index = getattr(self, "_rail_index", 0) + 1
            if line.lstrip().startswith(components.ResponseStream.FENCE):
                self._fenced = not getattr(self, "_fenced", False)
                self._plain(line)
                return
            if getattr(self, "_fenced", False) or not shimmer.colourful:
                self._plain(line)
                return
            body = Text()
            colour = tone(self._rail_index + shimmer.phase)
            stripped = line.lstrip()
            indent = line[:len(line) - len(stripped)]
            if stripped[:1] in ("-", "*", "+") and stripped[1:2] == " ":
                body.append(indent + BULLET + " ",
                            style=tone(self._list_step() + shimmer.phase))
                body.append(stripped[2:] + "\n")
            elif stripped.startswith("#") and stripped[1:2] == " ":
                head = stripped.lstrip("#").strip()
                for position in range(len(head)):
                    body.append(head[position],
                                style=across(position, len(head), shimmer.phase))
                body.append("\n")
            else:
                body.append(line + "\n")
            components.console.print(Text.assemble(Text(RAIL + " ", style=colour), body))

        def _plain(self, line: str) -> None:
            """The line as the model wrote it, with the rail and nothing else."""
            if not shimmer.colourful:
                components.console.print(Text(line))
                return
            rail = Text(RAIL + " ", style=tone(self._rail_index + shimmer.phase))
            components.console.print(Text.assemble(rail, Text(line + "\n")))

        def _list_step(self) -> int:
            """Spread the bullets of one answer across the ramp as the block does."""
            self._bullets = getattr(self, "_bullets", 0) + 1
            return int(self._bullets * (CYCLE - 1) / 24)

        def _print(self, text):
            if not text:
                return
            self._pending += components.strip_terminal(text)
            while True:
                line, found, rest = self._pending.partition("\n")
                if not found:
                    if len(line) >= self.LINE_FLUSH_CHARS:
                        self._pending = line[self.LINE_FLUSH_CHARS:] + rest
                        self._emit(line[:self.LINE_FLUSH_CHARS])
                        continue
                    return
                self._pending = rest
                self._emit(line)

        def _flush_pending(self):
            if self._pending:
                self._emit(self._pending)
                self._pending = ""

    _RailStream = RailStream
    return RailStream


# --- the opening banner and the panel frame ------------------------------------------

def banner() -> None:
    """The wordmark, painted across the ramp. One print: startup is already measured."""
    from beeagent import __version__
    from beeagent.ui.components import console

    line = Text("BeeCode")
    if shimmer.colourful:
        for position in range(len(line.plain)):
            line.stylize(across(position, len(line.plain), shimmer.phase),
                         position, position + 1)
    line.append("  v" + str(__version__), style="dim " + LEAF)
    console.print(line)


def frame_dict():
    """The heavy outline every classic panel asks for.

    One static sweep rather than a moving one: this value is registered once and
    read by whichever thread prints a panel, and Rich styles a box as a whole.
    """
    from rich import box

    return {"box": box.HEAVY, "border_style": "bold " + HONEY}


# --- the hooks the host reads ----------------------------------------------------------

def on_init(ctx) -> None:
    shimmer.reset()
    shimmer.lang = clean(getattr(ctx, "language", "en") or "en", 8) or "en"
    give = getattr(ctx, "translate", None) or getattr(ctx, "L", None)
    shimmer.translate = give if callable(give) else None
    painter = getattr(ctx, "painter", None)
    support = getattr(painter, "color_support", None)
    if callable(support):
        try:
            shimmer.colourful = str(support() or "") not in ("", "none")
        except Exception:
            shimmer.colourful = True


def on_surfaces(ctx):
    """Ask for what this screen can hold; a refused claim costs nothing."""
    try:
        cols, _rows = ctx.size()
    except Exception:
        cols = 80
    wanted = ["status", "spinner", "thinking", "answer"]
    if int(cols or 0) >= MIN_FRAME_COLS:
        wanted.append("hud")
    return wanted


def on_event(event, payload) -> None:
    shimmer.on_event(event, payload)


def on_output(text) -> None:
    if text:
        shimmer.lines += 1


def on_frame(dt, painter) -> None:
    shimmer.advance(dt)


def on_status(default):
    return shimmer.status(default or "")


def on_spinner(default, clock):
    return shimmer.spinner(default or "", clock)


def on_thinking(line):
    return shimmer.thinking(line or "")


def on_answer(text, final=False):
    return shimmer.answer(text, bool(final))


def on_hud(painter, dt):
    shimmer.hud(painter, dt)


def hooks() -> dict:
    return {"SURFACES": SURFACES, "HUD_ROWS": HUD_ROWS,
            "on_init": on_init, "on_surfaces": on_surfaces, "on_event": on_event,
            "on_output": on_output, "on_frame": on_frame, "on_status": on_status,
            "on_spinner": on_spinner, "on_thinking": on_thinking,
            "on_answer": on_answer, "on_hud": on_hud}


# --- registration ------------------------------------------------------------------------

def setup(api) -> None:
    """The two doors, in the order that costs least to get wrong.

    Slot variants are *chosen*, not merely registered: `set_skin` records the choice
    as a plugin's, so a user's own `/skin frame …` still wins and survives a reload.
    The `spinner` slot is deliberately left alone — that line is a surface, which
    both interfaces ask about, while the slot only exists in the classic one.
    """
    if not attach(api):
        for name in ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
                     "tool_denied", "done", "stopped", "error"):
            api.event(name, on_event)
    offer(api, "frame", frame_dict())
    offer(api, "banner", banner)
    offer(api, "stream", rail_stream())


def offer(api, slot: str, value) -> None:
    """Register a variant under this pack's name and ask the host to use it."""
    give = getattr(api, "skin", None)
    choose = getattr(api, "set_skin", None)
    if not callable(give):
        return
    try:
        give(slot, NAME, value)
        if callable(choose):
            choose(slot, NAME)
    except Exception:
        pass


def contribute(api) -> None:
    """Say so in `/extensions`, whichever route took the hooks."""
    give = getattr(api, "skin", None)
    if callable(give):
        try:
            give("skins", NAME, hooks())
        except Exception:
            pass


def attach(api) -> bool:
    """Hand the lifecycle to the host through whichever verb it answers to."""
    given = hooks()
    for verb in ("skin_hooks", "register_skin", "lifecycle", "hook"):
        run = getattr(api, verb, None)
        if callable(run):
            try:
                run(NAME, given)
                contribute(api)
                return True
            except TypeError:
                continue
            except Exception:
                continue
    try:
        import beeagent.core.skins as engine   # the full name, as the gate reads it
    except Exception:
        return False
    for verb in ("register", "register_skin", "attach"):
        run = getattr(engine, verb, None)
        if callable(run):
            try:
                run(NAME, given)
                return True
            except Exception:
                continue
    return False
