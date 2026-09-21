import math
import os
import sys
import time
from typing import Optional
from beeagent.ui import skin
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.text import Text
from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.table import Table
from rich.align import Align
from rich import box

from beeagent import __version__
from beeagent.i18n import L

from beeagent.core.parser import CommandParser

# Some Windows consoles / redirected streams use a non-UTF8 code page (e.g.
# cp1251) that cannot encode the block and emoji glyphs this UI draws. Replace
# unencodable characters instead of raising UnicodeEncodeError.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:
        pass

console = Console()
_parser = CommandParser()

# Chunky 5x5 pixel font used for the banner.
PIXEL_FONT = {
    "B": ["████ ", "█   █", "████ ", "█   █", "████ "],
    "E": ["█████", "█    ", "████ ", "█    ", "█████"],
    "C": [" ████", "█    ", "█    ", "█    ", " ████"],
    "O": [" ███ ", "█   █", "█   █", "█   █", " ███ "],
    "D": ["████ ", "█   █", "█   █", "█   █", "████ "],
    " ": ["     ", "     ", "     ", "     ", "     "],
}

GRADIENT = ["#fff59d", "#ffee58", "#c0ca33", "#7cb342", "#43a047"]

# --- BeeCode theme: the logo palette, drawn in bold -------------------------

HONEY = "#ffcc00"
LEAF = "#7cb342"
DARK_LEAF = "#43a047"
# Classic rounded frame, but heavy: the border carries the bold attribute and
# the honey->leaf colour instead of the plain default green.
BORDER = f"bold {LEAF}"
HEAVY_BORDER = f"bold {DARK_LEAF}"

# Ramp the shimmer sweeps through the banner: pale honey -> gold -> leaf.
SHIMMER_STOPS = ["#fff9c4", "#ffee58", "#ffcc00", "#f6a821", "#c0ca33", "#7cb342", "#43a047"]
FLASH = "#fffde7"          # a freshly revealed pixel glows almost white
BANNER_STEP_DELAY = 0.035
BANNER_REVEAL_STEPS = 16
BANNER_SETTLE_STEPS = 22
BANNER_WAVES = 1.6  # how many colour waves fit across the word
BANNER_GLOW_SPAN = 6  # columns a new pixel keeps glowing before it settles


def _rgb(color: str) -> tuple:
    return tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))


_SHIMMER_RGB = [_rgb(c) for c in SHIMMER_STOPS]


def _shimmer_color(pos: float) -> str:
    """Colour at `pos` (0..1) on the honey->leaf ramp, linearly interpolated."""
    last = len(_SHIMMER_RGB) - 1
    x = min(max(pos, 0.0), 1.0) * last
    i = min(int(x), last - 1)
    t = x - i
    a, b = _SHIMMER_RGB[i], _SHIMMER_RGB[i + 1]
    return "#%02x%02x%02x" % tuple(
        round(a[k] + (b[k] - a[k]) * t) for k in range(3)
    )


def _blend(c1: str, c2: str, t: float) -> str:
    """Colour `t` of the way from hex colour c1 to hex colour c2."""
    a, b = _rgb(c1), _rgb(c2)
    return "#%02x%02x%02x" % tuple(
        round(a[k] + (b[k] - a[k]) * t) for k in range(3)
    )


def _pixel_text(word: str, xscale: int = 1, sep: str = " ") -> list[str]:
    """Render a word as 5 rows of blocky pixel art.

    xscale stretches the art horizontally (each column becomes `xscale` wide).
    """
    rows = []
    for r in range(5):
        cells = [PIXEL_FONT.get(ch, PIXEL_FONT[" "])[r] for ch in word]
        cells = ["".join(c * xscale for c in cell) for cell in cells]
        rows.append(sep.join(cells))
    return rows


BANNER_ROWS = _pixel_text("BEECODE", xscale=2)
BANNER = "\n".join(BANNER_ROWS)
BANNER_WIDTH = max(len(r) for r in BANNER_ROWS)


def _banner_paint(pick) -> Text:
    """Render the logo; `pick(row, col, width)` returns a colour or None.

    None means "still hidden" — the cell is emitted as a blank so the art
    never changes shape between frames, only its colours.
    """
    text = Text()
    last = len(BANNER_ROWS) - 1
    for i, row in enumerate(BANNER_ROWS):
        for j, ch in enumerate(row):
            if ch == " ":
                text.append(" ")
                continue
            color = pick(i, j, BANNER_WIDTH)
            text.append(ch if color else " ", style=f"bold {color}" if color else "")
        if i != last:
            text.append("\n")
    return text


def _rest_color(row: int) -> str:
    """The colour a pixel has once the logo is at rest."""
    return GRADIENT[row % len(GRADIENT)]


def _wave_color(row: int, col: int, width: int, phase: float) -> str:
    """Colour of one pixel under the shimmer wave at `phase` (0..1 per cycle)."""
    wave = 0.5 + 0.5 * math.cos(
        2 * math.pi * (col / width * BANNER_WAVES + row / 9 - phase)
    )
    return _shimmer_color(wave)


def banner_reveal_frame(front: float) -> Text:
    """First act: the word materialises left-to-right, new pixels glowing.

    Pixels cool from the flash into the wave's colour, so this act hands over to
    the second one without a jump in colour.
    """
    def pick(i, j, width):
        age = front - j
        if age < 0:
            return None
        return _blend(FLASH, _wave_color(i, j, width, 0.0), min(1.0, age / BANNER_GLOW_SPAN))
    return _banner_paint(pick)


def banner_frame(phase: float) -> Text:
    """A shimmer frame: a colour wave travelling left-to-right through the logo.

    `phase` runs 0..1 for a full cycle, so consecutive frames read as motion.
    """
    return _banner_paint(lambda i, j, width: _wave_color(i, j, width, phase))


def banner_settle_frame(phase: float, fade: float) -> Text:
    """Second act: the wave keeps rolling while easing into the resting palette.

    `fade` 0..1; at 1 the picture is exactly `banner_static()`.
    """
    def pick(i, j, width):
        return _blend(_wave_color(i, j, width, phase), _rest_color(i), fade)
    return _banner_paint(pick)


def banner_static() -> Text:
    """The resting logo: the classic row-by-row gradient."""
    return _banner_paint(lambda i, j, width: _rest_color(i))


def brand_ramp(text: str) -> list:
    """`text` split into (style, char) tokens along the honey->leaf ramp.

    prompt_toolkit widgets take the tokens directly; rich wraps them in a Text.
    """
    n = max(1, len(text) - 1)
    return [(f"bold {_shimmer_color(k / n)}", ch) for k, ch in enumerate(text)]


def bee_title(text: str) -> Text:
    """A heading painted with the honey->leaf ramp, letter by letter."""
    out = Text()
    for style, ch in brand_ramp(text):
        out.append(ch, style=style)
    return out


def _banner_animates(animate: Optional[bool]) -> bool:
    if animate is None:
        animate = os.environ.get("BEECODE_NO_ANIM") not in ("1", "true", "yes")
    return bool(animate) and console.is_terminal


def _banner_frames() -> list:
    """Reveal -> the same wave rolling on and cooling into the resting palette."""
    front = BANNER_WIDTH + BANNER_GLOW_SPAN
    frames = [
        banner_reveal_frame(k / BANNER_REVEAL_STEPS * front)
        for k in range(BANNER_REVEAL_STEPS + 1)
    ]
    # Act 1 hands over at wave phase 0, so act 2 continues from there; frame 0
    # of the blend is the frame act 1 ended on, hence start at 1.
    frames += [
        banner_settle_frame(0.5 * k / BANNER_SETTLE_STEPS, k / BANNER_SETTLE_STEPS)
        for k in range(1, BANNER_SETTLE_STEPS + 1)
    ]
    return frames


def print_banner(animate: Optional[bool] = None):
    """Draw the logo: it appears pixel by pixel, then its colours settle down.

    Which of those happen is the `banner` slot: "shimmer" animates, "static"
    prints the settled logo, "none" prints nothing at all.
    """
    mode = skin.get("banner") or "shimmer"
    if mode == "none":
        return
    if console.is_terminal and mode == "shimmer":
        # Wipe the screen at launch so the logo lands on a clean page.
        console.clear()
    console.print()
    if mode == "shimmer" and _banner_animates(animate):
        # Runs before the prompt starts, so Live's cursor movement cannot
        # fight with patch_stdout; `transient` erases the frames afterwards.
        try:
            with Live(console=console, transient=True, auto_refresh=False) as live:
                for frame in _banner_frames():
                    live.update(Align.center(frame), refresh=True)
                    time.sleep(BANNER_STEP_DELAY)
        except Exception:
            pass
    console.print(Align.center(banner_static()))
    console.print()
    console.print(Align.center(
        Text.assemble(("BeeCode", f"bold {HONEY}"), Text(" — free AI coding agent powered by g4f", style="dim"))
    ))
    console.print(Align.center(Text(f"v{__version__}", style="dim italic")))
    console.print()

def print_welcome():
    console.print()
    panel = Panel(
        Align.center(Text("Type a request, or '/' for commands — 'quit' to exit", style="dim")),
        **skin.frame_kwargs(BORDER),
        padding=(0, 2),
    )
    console.print(panel)
    console.print()

def render_tool_start(tool_name: str, tool_args: dict):
    args_str = ", ".join(f"{k}={v!r}" for k, v in tool_args.items())
    if len(args_str) > 60:
        args_str = args_str[:57] + "..."

    text = Text()
    text.append("  ⏳ ", style="bold")
    text.append(f"{tool_name}", style="bold #ffcc00")
    text.append(f" {args_str}", style="dim")
    console.print(text)

def render_tool_end(tool_name: str, tool_args: dict, output: str, error: bool):
    if error:
        icon = "❌"
        color = "red"
    else:
        icon = "✅"
        color = "green"

    if tool_name == "write" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold #7cb342]●[/] [#8fbf6f]created file[/] [dim]{path}[/]")
        _render_code_preview(tool_args.get("content", ""))
    elif tool_name == "edit" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold #7cb342]●[/] [#8fbf6f]modified[/] [dim]{path}[/]")
    elif tool_name == "bash" and not error:
        cmd = tool_args.get("command", "")[:60]
        console.print(f"    [bold #7cb342]●[/] [#8fbf6f]executed[/] [dim]{cmd}[/]")
        if output.strip():
            _render_bash_output(output)
    elif tool_name == "read" and not error:
        path = tool_args.get("path", "file")
        console.print(f"    [bold #ffd54f]●[/] [#8fbf6f]read[/] [dim]{path}[/]")
    elif tool_name == "grep" and not error:
        pattern = tool_args.get("pattern", "")
        console.print(f"    [bold #ffd54f]●[/] [#8fbf6f]searched[/] [dim]{pattern}[/]")
    elif tool_name == "glob" and not error:
        pattern = tool_args.get("pattern", "")
        console.print(f"    [bold #ffd54f]●[/] [#8fbf6f]found files[/] [dim]{pattern}[/]")
    elif tool_name == "git" and not error:
        cmd = tool_args.get("command", "")[:60]
        console.print(f"    [bold #ffcc00]●[/] [#c0ca33]git[/] [dim]{cmd}[/]")
    elif tool_name == "web_search" and not error:
        query = tool_args.get("query", "")[:60]
        console.print(f"    [bold #7cb342]●[/] [#8fbf6f]searched web[/] [dim]{query}[/]")
    elif tool_name == "todo" and not error:
        action = tool_args.get("action", "")
        console.print(f"    [bold #ffcc00]●[/] [#ffd54f]todo:[/] [dim]{action}[/]")
    else:
        console.print(f"    [bold {color}]{icon}[/] [{color}]{tool_name}[/]")

    if error and output:
        console.print(f"    [red]{output[:200]}[/]")

def _render_code_preview(content: str):
    if not content:
        return
    lines = content.split("\n")
    preview = lines[:12]
    if len(lines) > 12:
        preview.append(f"  ... ({len(lines) - 12} more lines)")

    code = "\n".join(preview)
    syntax = Syntax(code, "python", theme="monokai", line_numbers=False)
    panel = Panel(
        syntax,
        **skin.frame_kwargs(BORDER),
        padding=(0, 1),
    )
    console.print(panel)

def _render_bash_output(output: str):
    lines = output.strip().split("\n")
    preview = lines[:8]
    if len(lines) > 8:
        preview.append(f"  ... ({len(lines) - 8} more lines)")

    text = "\n".join(preview)
    panel = Panel(
        Text(text, style="dim"),
        **skin.frame_kwargs(HEAVY_BORDER),
        padding=(0, 1),
    )
    console.print(panel)

def render_response(text: str):
    console.print()
    md = Markdown(text)
    panel = Panel(
        md,
        **skin.frame_kwargs(BORDER),
        title=bee_title("🐝 BeeCode"),
        title_align="left",
        padding=(0, 1),
    )
    console.print(panel)
    console.print()

def render_error(message: str):
    console.print()
    panel = Panel(
        Text(message, style="red"),
        **skin.frame_kwargs("bold red"),
        title="[bold red]Error[/]",
        title_align="left",
    )
    console.print(panel)
    console.print()

def render_economy_hit():
    text = Text()
    text.append("  💾 ", style="bold")
    text.append("cache hit", style="bold green")
    text.append(" — response served from cache", style="dim")
    console.print(text)


def render_tool_denied(tool: str, args: dict):
    """A tool the user never granted: loud, but recoverable."""
    shown = " ".join(f"{k}={str(v)[:40]!r}" for k, v in list(args.items())[:3])
    text = Text()
    text.append("  ⛔ ", style="bold")
    text.append(f"{tool}", style="bold #ffcc00")
    if shown:
        text.append(f" {shown}", style="dim")
    text.append("  blocked — no permission", style="bold red")
    console.print(text)
    console.print(Text(L(
        f"     allow it: /allow {tool}   or /permissions auto to trust the model",
        f"     разрешить: /allow {tool}   либо /permissions auto, чтобы доверять модели"),
        style="dim"))


def render_model_switched(model_from: str, model_to: str):
    text = Text()
    text.append("  🔄 ", style="bold")
    text.append(L(f"this provider has no “{model_from}” — answering with ",
                  f"у этого провайдера нет «{model_from}» — отвечаем "), style="dim")
    text.append(model_to, style="bold #ffcc00")
    console.print(text)

def render_model_info(model: str, provider: str, mode: str, permissions: str = ""):
    text = Text()
    text.append("  🐝 ", style="bold")
    text.append(f"model: ", style="dim")
    text.append(f"{model}", style="bold #ffcc00")
    text.append(f"  provider: ", style="dim")
    text.append(f"{provider}", style="bold #ffcc00")
    text.append(f"  mode: ", style="dim")
    text.append(f"{mode}", style="bold #ffcc00" if mode == "normal" else "bold yellow")
    if permissions and permissions != "auto":
        # Say up front why a tool gets blocked, before the first refusal.
        text.append(f"  permissions: ", style="dim")
        text.append(f"{permissions}", style="bold yellow")
        text.append(L("   /allow <tool> · /permissions auto",
                      "   /allow <инструмент> · /permissions auto"), style="dim")
    console.print(text)

def models_table(models: list[str]) -> Table:
    table = Table(
        title=bee_title("🐝 Models"),
        **skin.frame_kwargs(BORDER),
        show_header=True,
        header_style="bold " + HONEY,
        expand=False,
    )
    table.add_column("#", style="dim", width=4)
    table.add_column("Model", style="bold #ffcc00")
    table.add_column("Provider", style="dim")
    for i, model in enumerate(models, 1):
        table.add_row(str(i), model, "g4f")
    return table


def providers_table(providers: list[dict]) -> Table:
    table = Table(
        title=bee_title("🐝 Providers"),
        **skin.frame_kwargs(BORDER),
        show_header=True,
        header_style="bold " + HONEY,
        expand=False,
    )
    table.add_column("Name", style="bold #ffcc00")
    table.add_column("Type", style="dim")
    table.add_column("Description")
    for p in providers:
        table.add_row(p["name"], p["type"], p["desc"])
    return table


def commands_table(commands: list) -> Table:
    table = Table(
        title=bee_title("🐝 Commands"),
        **skin.frame_kwargs(BORDER),
        show_header=True,
        header_style="bold " + HONEY,
        expand=False,
    )
    table.add_column("Command", style="bold #ffcc00")
    table.add_column("Description")
    table.add_column("Usage", style="dim")
    for c in commands:
        table.add_row("/" + c.name, c.description, c.usage or "")
    return table


def tools_table(tools: list) -> Table:
    table = Table(
        title=bee_title("🐝 Tools"),
        **skin.frame_kwargs(BORDER),
        show_header=True,
        header_style="bold " + HONEY,
        expand=False,
    )
    table.add_column("Tool", style="bold #ffcc00")
    table.add_column("Description")
    for t in tools:
        table.add_row(t.name, t.description)
    return table


def print_models(models: list[str]):
    console.print()
    console.print(models_table(models))
    console.print()


def print_providers(providers: list[dict]):
    console.print()
    console.print(providers_table(providers))
    console.print()


# --- playful pending states shown while the request travels to the model ---

# English and Russian side by side, so a joke never exists in one language only.
PENDING_STATES = [
    ("prepares pelmeni for breakfast...", "готовит пельмешки на завтрак..."),
    ("brews tea for the bees...", "заваривает чай для пчёлок..."),
    ("pondering combs and code...", "размышляет о сотах и коде..."),
    ("polishes the hive until it shines...", "полирует улей до блеска..."),
    ("buzzing over the keyboard...", "жужжит над клавиатурой..."),
    ("storing honey for winter...", "делает запасы мёда на зиму..."),
    ("counting pollen by modulo...", "считает пыльцу по модулю..."),
    ("turning nectar into tokens...", "переводит нектар в токены..."),
    ("stamping the hive deadline...", "чекает дедлайн улья..."),
    ("wiping honey off the keyboard...", "чистит клавиатуру от мёда..."),
]


def pending_text() -> str:
    """What the "working" line says — or nothing, when the spinner is off."""
    import random

    mode = skin.get("spinner") or "honey"
    if mode == "none":
        return ""
    if mode == "dots":
        return "…"
    return L(*random.choice(PENDING_STATES))


# --- thinking blocks -------------------------------------------------------

LAST_THINKING = ""  # full text of the most recent reasoning block


class ResponseStream:
    """Line-oriented response renderer with a collapsible thinking tail.

    Plain writes only — no rich Live / cursor movement — so output stays clean
    while the prompt_toolkit prompt remains active under patch_stdout (the user
    can keep typing while the agent works; input goes to the pending queue).

    While thinking tokens stream in, a dim "💭 думает..." header appears; F2
    (peek) opens the last THINKING_VISIBLE_LINES lines and keeps streaming the
    rest live. Reasoning is kept in LAST_THINKING and can be reviewed via
    /thinking (a pager you scroll). Answer chunks stream live, except tool
    calls: a payload the parser would execute is never shown (see `_feed`).
    """

    THINKING_VISIBLE_LINES = 10
    FENCE = "```"
    FENCE_MAX = 6000        # a never-closing fence this long is printed anyway
    LINE_FLUSH_CHARS = 240  # a line this long is broken rather than kept hidden

    def __init__(self):
        self._phase = "idle"        # idle | status | thinking | content
        self._text = ""             # answer content collected this turn
        self._thinking = ""         # reasoning text collected this turn
        self._think_live = False    # user opened the live thinking tail (F2)
        self._think_printed = 0     # complete thinking lines already printed
        self._content_started = False
        self._buf = ""              # text held back while a fence is scanned
        self._in_fence = False      # inside a ``` block, waiting for the close
        self._lead_json = None      # undecided until real text arrives
        self._lead_buf = ""
        self._pending = ""          # the answer line still being written

    # -- internals ----------------------------------------------------------

    def _begin_turn(self):
        self._phase = "idle"
        self._text = ""
        self._thinking = ""
        self._think_printed = 0
        self._content_started = False
        self._buf = ""
        self._in_fence = False
        self._lead_json = None      # undecided until real text arrives
        self._lead_buf = ""
        self._pending = ""
        # F2 opens the reasoning tail for *this* answer. Left set, every later
        # turn streamed all of its thinking to the screen unasked.
        self._think_live = False

    def _store_thinking(self):
        global LAST_THINKING
        LAST_THINKING = self._thinking

    def _complete_think_lines(self) -> list:
        # split always yields a trailing element (empty when the buffer ends
        # with \n, the still-growing chunk otherwise) — drop it.
        return self._thinking.split("\n")[:-1]

    def _print_think_lines(self):
        for line in self._complete_think_lines()[self._think_printed:]:
            console.print(Text("  │ " + line, style="#7a8f7a"))
        self._think_printed = len(self._complete_think_lines())

    # -- public API ---------------------------------------------------------

    def on_status(self):
        """The request was sent; show a playful pending state."""
        if self._thinking.strip():
            self._store_thinking()
        # Anything the model already said must reach the screen before the next
        # turn's header — otherwise a nudged retry would swallow it silently.
        self._flush_pending()
        self._begin_turn()
        self._phase = "status"
        pending = pending_text()
        if not pending:
            return                      # spinner off: no line at all
        header = Text("  💬 ", style="bold")
        header.append(pending, style="dim italic")
        console.print(header)

    def on_thinking(self, text):
        if self._phase != "thinking":
            self._phase = "thinking"
            console.print(Text(L("  💭 thinking...", "  💭 думает..."), style="bold #ffcc00"))
        self._thinking += text
        if self._think_live:
            self._print_think_lines()

    def on_content(self, text):
        if not self._content_started:
            self._content_started = True
            self._phase = "content"
        self._text += text
        if self._lead_json is None and self._text.strip():
            # A reply that opens with a bare JSON object is a tool call. The
            # decision waits for the first non-space character: providers pass
            # whitespace through, and one leading space used to settle it as
            # "not a payload" for the whole turn — after which the JSON streamed
            # to the screen right before the tool ran.
            self._lead_json = self._text.lstrip()[:1] == "{"
        if self._lead_json:
            self._lead_buf += text
            return
        self._feed(text)

    def _feed(self, text):
        """Print answer text live, but never a fenced tool-call payload.

        A model that wants a tool often writes a sentence first and then the
        ```json block; the block is buffered until the fence closes and is
        printed only when it is a real code sample, not a command.
        """
        self._buf += text
        while True:
            if self._in_fence:
                close = self._buf.find(self.FENCE, len(self.FENCE))
                if close == -1:
                    if len(self._buf) > self.FENCE_MAX:
                        if _parser.parse(self._buf).has_commands:
                            # A payload the model never closed the fence on is
                            # still going to run, so a 7 KB HTML dump has no
                            # business on the screen. Keep holding the rest.
                            self._buf = ""
                        else:
                            self._print(self._buf)
                            self._buf = ""
                            self._in_fence = False
                    return
                block = self._buf[:close + len(self.FENCE)]
                self._buf = self._buf[close + len(self.FENCE):]
                self._in_fence = False
                if not _parser.parse(block).has_commands:
                    self._print(block)
                continue
            open_at = self._buf.find(self.FENCE)
            if open_at == -1:
                keep = self._partial_fence_len(self._buf)
                if len(self._buf) > keep:
                    self._print(self._buf[:len(self._buf) - keep])
                    self._buf = self._buf[len(self._buf) - keep:]
                return
            if open_at:
                self._print(self._buf[:open_at])
                self._buf = self._buf[open_at:]
            self._in_fence = True

    @classmethod
    def _partial_fence_len(cls, buf: str) -> int:
        """Trailing characters that could still grow into a fence opener."""
        for k in (2, 1):
            if buf.endswith(cls.FENCE[:k]):
                return k
        return 0

    def _print(self, text):
        """Write only whole lines — never leave the cursor mid-line.

        While the prompt is active, prompt_toolkit owns the screen: a write that
        stops in the middle of a line leaves the cursor where its model does not
        know about, and the next repaint of the prompt overwrites the beginning
        of that line. That is the "answer lost its first letters" bug, and the
        one-write-per-token habit behind it is where the stutter came from.
        """
        if not text:
            return
        self._pending += text
        while True:
            line, found, rest = self._pending.partition("\n")
            if not found:
                if len(line) >= self.LINE_FLUSH_CHARS:
                    self._pending = line[self.LINE_FLUSH_CHARS:] + rest
                    console.print(Text(line[:self.LINE_FLUSH_CHARS]))
                    continue
                return
            self._pending = rest
            console.print(Text(line))

    def _flush_pending(self):
        """End the turn on a line boundary, whatever is left in the buffer."""
        if self._pending:
            console.print(Text(self._pending))
            self._pending = ""

    def on_tool_start(self):
        """Content so far was a tool-call payload, not an answer — drop it."""
        holding = self._lead_json or self._in_fence
        if self._content_started and not holding:
            self._print(self._buf)
            self._flush_pending()
        self._buf = ""
        self._in_fence = False
        self._lead_json = None      # undecided until real text arrives
        self._lead_buf = ""
        self._content_started = False
        self._text = ""
        self._phase = "idle"
        self._pending = ""

    def on_done(self):
        pending = self._lead_buf if self._lead_json else self._buf
        if pending.strip() and not _parser.parse(pending).has_commands:
            self._print(pending)
        self._flush_pending()
        if self._thinking.strip():
            self._store_thinking()
            n = len(self._thinking.strip().split("\n"))
            console.print(Text(
                L(f"  💭 reasoning: {n} lines  (/thinking for all of it, F2 for the tail)",
                  f"  💭 размышления: {n} строк  (/thinking — полностью, F2 — хвост)"),
                style="dim",
            ))
        answered = self._text
        if not answered.strip() and not self._thinking.strip():
            console.print(Text(
                L("  ⚠ the model returned an empty answer — try again or switch model (/models)",
                  "  ⚠ модель вернула пустой ответ — попробуй ещё раз или смени модель (/models)"),
                style="#ffcc00",
            ))
        self._begin_turn()

    def on_response(self, text: str):
        """A ready answer arrived outside streaming (cache hit)."""
        if text and text.strip():
            render_response(text)

    def on_error(self, msg: str):
        if self._content_started:
            self._flush_pending()
        render_error(msg)
        self._begin_turn()

    def on_reset(self):
        """A partial stream was abandoned; drop its fragments before a retry.

        Thinking is kept (it stays valid context for the user); only the
        answer-side buffers are cleared so the next attempt's text prints
        from scratch instead of duplicating what already hit the screen.
        """
        self._text = ""
        self._content_started = False
        self._buf = ""
        self._in_fence = False
        self._lead_json = None      # undecided until real text arrives
        self._lead_buf = ""
        self._pending = ""

    def thinking(self) -> str:
        """Reasoning collected this turn, or the last completed block."""
        return self._thinking.strip() or LAST_THINKING.strip()

    def peek(self):
        """F2: open the last THINKING_VISIBLE_LINES lines, then stream live."""
        text = self.thinking()
        if not text:
            console.print(Text(
                L("  💭 (no reasoning yet — models share their thoughts only sometimes)",
                  "  💭 (размышлений пока нет — модели отдают мысли только иногда)"),
                style="dim",
            ))
            return
        lines = text.split("\n")
        shown = lines[-self.THINKING_VISIBLE_LINES:]
        console.print(Text(
            L(f"  💭 ── tail of the reasoning ({len(shown)} lines) ──",
              f"  💭 ── хвост мыслей ({len(shown)} строк) ──"), style="#4c6b4c"))
        for line in shown:
            console.print(Text("  │ " + line, style="#7a8f7a"))
        self._think_live = True
        self._think_printed = len(self._complete_think_lines())

    def reset(self):
        self._begin_turn()


_stream = None


def get_stream() -> ResponseStream:
    global _stream
    if _stream is None:
        _stream = ResponseStream()
    return _stream


def thinking_body() -> str:
    """Full reasoning text shown by /thinking and F2's viewer."""
    return get_stream().thinking()


def show_thinking_fallback():
    """Dump the last reasoning block plainly.

    Only used when the full-screen viewer cannot run (piped output, no
    console). rich's `console.pager()` is deliberately avoided: on the Windows
    console it only advances on Enter, which is what the user could not scroll.
    """
    text = thinking_body()
    if not text:
        console.print(Text(
            L("(no saved reasoning yet — models share their thoughts only sometimes)",
              "(пока нет сохранённых размышлений — модели отдают мысли только иногда)"),
            style="dim",
        ))
        return
    lines = text.rstrip().split("\n")
    numbered = "\n".join(f"{i:>4} │ {line}" for i, line in enumerate(lines, 1))
    console.print(Panel(
        Text(numbered),
        title=f"💭 мысли ({len(lines)} строк)",
        title_align="left",
        **skin.frame_kwargs(BORDER),
        padding=(0, 1),
    ))
