"""`skin-hud` — the surface reference: a skin that takes responsibility for lines
the host would otherwise write.

`skin-baseline` shows a status line, `skin-pulse` shows animation. This one shows
the third thing an author can ask for: *ownership*. It claims four of the five
surfaces and answers for each:

* `status` — the state line, in the skin's own words, with the numbers the host
  already had, as Rich markup;
* `spinner` — the waiting line, phased by the monotonic clock the host hands over
  instead of a phrase picked out of a hat;
* `thinking` — the reasoning block while it is on the screen. This one answers in
  **plain text**, because the host prints these lines inside its own `Text`, and a
  markup tag there would show up as letters;
* `hud` — the reserved strip above the state line, drawn through the painter: the
  state on the left, how long this turn has been running in the middle, and what
  the agent is doing on the right, scrolling if the name does not fit.

`stream` is deliberately **not** claimed. That surface is the model's own bytes on
their way to the log; a skin that rewrites them is a skin that hides the answer,
and nothing drawn on top of that is worth it.

Two rules this pack exists to teach:

1. **Ask for what you can draw.** `on_surfaces` answers with the list filtered
   against the screen the host reports, so a 30-column terminal gets a text status
   line rather than a HUD that cannot fit one. A refused claim costs nothing; a
   kept one that draws garbage costs the surface and the user's trust.
2. **Every widget is optional.** The painter in the contract has six methods; the
   bars and tickers are a later addition. Each call here goes through `paint()`,
   which falls back to `draw_text` when the build underneath has no such method, so
   the same pack works on an older host and on a newer one.

Frames stay deterministic: `dt` decides what moves, so a test can advance a whole
turn without waiting for a clock. Where the terminal cannot do colour the markup
still reads, because the strings are text first and colour second — and ASCII
glyphs throughout, since a cp1251 console has no block characters to give.
"""
import beeagent.core.renderer as R

NAME = "hud"

#: The static form: the host reads this attribute when the module has no
#: `on_surfaces`. This pack defines both, and the hook wins.
SURFACES = ("status", "spinner", "thinking", "hud")

#: Rows asked for. The host clamps at four and says so out loud when it cuts.
HUD_ROWS = 1

HONEY = "#ffcc00"
LEAF = "#7cb342"
DEEP = "#2b2113"        # the hive's own dark, for the label's background
AMBER = "#ff8a00"       # a tool that went wrong

#: A turn longer than this has its bar full and waiting — the strip says "this is
#: taking a while", which is true, and never "almost done", which would not be.
TURN_SPAN = 30.0

WORDS = {
    "idle": ("idle", "в покое"),
    "thinking": ("thinking", "думаю"),
    "writing": ("writing", "пишу"),
    "tool": ("running", "выполняю"),
    "denied": ("refused", "отказ"),
    "failed": ("failed", "ошибка"),
    "ran": ("answered", "ответил"),
}

STATE = {
    "idle": "idle",
    "thinking": "thinking",
    "writing": "writing",
    "tool": "tool",
    "denied": "denied",
    "failed": "failed",
    "ran": "ran",
}

#: Narrowest screen the strip is worth reserving a row for. Below it the label
#: alone would eat the line and the numbers would not fit.
MIN_HUD_COLS = 40


def clean(text, limit: int = 32) -> str:
    """Event payload as screen ink: printable, one line, bounded.

    Tool names and arguments are text a model wrote. Without the strip, an
    argument carrying an escape sequence would land in the HUD and do whatever
    that sequence does to the terminal.
    """
    kept = [ch for ch in str(text) if 32 <= ord(ch) != 127][:limit]
    return "".join(kept).strip()


def paint(painter, widget: str, *args, **kwargs) -> bool:
    """`painter.<widget>()` when this build has it; False when it does not.

    The six documented methods are a floor, not a ceiling, and a pack that calls a
    newer widget blind raises on the host it was not written against — which the
    engine reports as "this skin lost the hud surface", not as "old host".
    """
    call = getattr(painter, widget, None)
    if not callable(call):
        return False
    try:
        call(*args, **kwargs)
    except Exception:
        return False
    return True


class Hud:
    """The four answers, and the counts behind them."""

    def __init__(self):
        self.lang = "en"
        self.translate = None
        self.state = "idle"
        self.tool = ""
        self.pieces = 0
        self.tokens = 0
        self.clock = 0.0        # seconds of this turn, from dt only
        self.turns = 0
        self.history = []

    def reset(self) -> None:
        """A fresh turn: the strip describes *this* request, not the last one."""
        self.pieces = 0
        self.tokens = 0
        self.clock = 0.0
        self.tool = ""
        self.history = []
        self.state = "idle"

    def say(self, english: str, russian: str) -> str:
        """Two languages, one call.

        `beeagent.i18n` is not on the host's allow-list, so a pack may not import
        `L`. The translator arrives on the context when the engine has one to
        hand out; until then this is the same rule with the same default.
        """
        if self.translate is not None:
            try:
                return self.translate(english, russian)
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    def word(self, key: str = "") -> str:
        return self.say(*WORDS[STATE.get(key or self.state, "idle")])

    # -- time and events -------------------------------------------------------

    def advance(self, dt) -> None:
        try:
            step = float(dt)
        except (TypeError, ValueError):
            step = 0.0
        if step != step or step < 0:      # NaN, and a clock that runs backwards
            step = 0.0
        self.clock += step

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
            self.state = "ran" if not data.get("error") else "failed"
            self.tool = ""
        elif event == "tool_denied":
            self.state = "denied"
            self.tool = clean(data.get("tool", "?"), 24)
        elif event == "retry":
            self.tool = clean(data.get("reason", "") or "retry", 24)
        elif event == "error":
            self.state = "failed"
        elif event == "done":
            self.state = "ran"
            self.tool = ""
        elif event == "stopped":
            self.state = "idle"
            self.reset()

    # -- the three text surfaces -------------------------------------------------

    def counts(self) -> str:
        """Tokens this turn, and their rate once there is a turn to divide by."""
        if not self.tokens:
            return ""
        if self.clock <= 0.5:
            return "%dt" % self.tokens
        return "%dt %.0f/s" % (self.tokens, self.tokens / self.clock)

    def status(self, default: str) -> str:
        """The state line: our word, the turn's numbers, then whatever the host said."""
        line = R.markup(self.word(), HONEY, style="bold")
        if self.tool:
            line += " " + R.markup(self.tool, LEAF)
        numbers = self.counts()
        if numbers:
            line += " " + R.markup(numbers, "grey42")
        return line + ((" · " + default) if default else "")

    def spinner(self, default: str, clock: float) -> None:
        """The waiting line, phased by the clock the host is already keeping.

        `None` here would mean "keep yours"; this answers with the host's own words
        made ours, because the phrase is a joke the user reads and the colour is
        ours to choose.
        """
        if not default:
            return None
        walk = int(R.phase(clock, 1.6) * 6)
        return R.ramp_markup(default, HONEY, LEAF) + " " + "·" * (walk % 4)

    def thinking(self, line: str) -> str:
        """Plain text, not markup: the host styles these lines itself.

        The marker is «», not curly quotes: a cp1251 console has the guillemets
        and not the quotes, and a marker that cannot print is worse than none.
        """
        if not line:
            return line
        return "« " + line

    # -- the grid surface ---------------------------------------------------------

    def fraction(self) -> float:
        """How far into `TURN_SPAN` this turn has run — the only honest bar here."""
        return min(1.0, self.clock / TURN_SPAN)

    def hud(self, painter, dt: float) -> None:
        cols, _rows = 80, 1
        size = getattr(painter, "get_terminal_size", None)
        if callable(size):
            try:
                got, _ = size()
                cols = max(1, int(got))
            except Exception:
                pass
        if cols < MIN_HUD_COLS:
            return
        if not paint(painter, "clear_region", 0, 0, cols, 1):
            return
        label = " " + self.word() + " "
        numbers = self.counts()
        bar_x = len(label) + 1
        bar_w = min(18, max(6, cols // 4))
        numbers_x = max(bar_x + bar_w + 2, cols - len(numbers))
        paint(painter, "draw_text", numbers_x, 0, numbers, color=LEAF)
        filled = int(round(bar_w * self.fraction()))
        if not paint(painter, "draw_bar", bar_x, 0, bar_w, self.fraction(),
                     color=HONEY, filled="#", empty="."):
            paint(painter, "draw_text", bar_x, 0, "#" * filled + "." * (bar_w - filled),
                  color=HONEY)
        paint(painter, "draw_text", 0, 0, label, color=HONEY, bg=DEEP, style="bold")
        tool_x = bar_x + bar_w + 1
        room = numbers_x - tool_x - 1
        if not self.tool or room < 6:
            return
        if R.text_cells(self.tool) > room and paint(
                painter, "draw_ticker", tool_x, 0, room, self.tool,
                phase=R.phase(self.clock, 4.0), color=AMBER):
            return                        # the name scrolls: this painter can draw it
        paint(painter, "draw_text", tool_x, 0, self.tool[:room], color=AMBER)


# --- module state: one instance, rebuilt when the host (re)initialises ----------

hud = Hud()


# --- the hooks the host reads ----------------------------------------------------

def on_init(ctx) -> None:
    hud.reset()
    hud.lang = clean(getattr(ctx, "language", "en") or "en", 8) or "en"
    give = getattr(ctx, "translate", None) or getattr(ctx, "L", None)
    hud.translate = give if callable(give) else None


def on_surfaces(ctx):
    """Claim only what this screen can hold.

    The host asks this once, when the skin is switched on. A claim refused here
    never costs anything: the line simply stays BeeCode's, and `/skins` says which
    surfaces the skin is on the hook for.
    """
    try:
        cols, _rows = ctx.size()
    except Exception:
        cols = 80
    wanted = ["status", "spinner", "thinking"]
    if int(cols or 0) >= MIN_HUD_COLS:
        wanted.append("hud")
    return wanted


def on_event(event, payload) -> None:
    hud.on_event(event, payload)


def on_output(text) -> None:
    if text:
        hud.on_event("stream_delta", {"text": text})


def on_frame(dt, painter) -> None:
    hud.advance(dt)


def on_status(default):
    return hud.status(default or "")


def on_spinner(default, clock):
    return hud.spinner(default or "", clock)


def on_thinking(line):
    return hud.thinking(line or "")


def on_hud(painter, dt):
    hud.hud(painter, dt)


def hooks() -> dict:
    """This pack's lifecycle, by name — the thing `setup` hands to the host."""
    return {"on_init": on_init, "on_surfaces": on_surfaces, "on_event": on_event,
            "on_output": on_output, "on_frame": on_frame, "on_status": on_status,
            "on_spinner": on_spinner, "on_thinking": on_thinking,
            "on_hud": on_hud}


# --- registration ------------------------------------------------------------------

def setup(api) -> None:
    if not attach(api):
        for name in ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
                     "tool_denied", "retry", "done", "stopped", "error"):
            api.event(name, on_event)
    contribute(api)


def contribute(api) -> None:
    """Say so in `/extensions`, whichever route took the hooks.

    Guarded, because the stand-in hosts in the tests hand over lifecycle hooks or
    an event bus and have no `skin` verb at all — and a pack must never raise
    inside `setup`, which the loader would report as a load error.
    """
    give = getattr(api, "skin", None)
    if callable(give):
        try:
            give("skins", NAME, hooks())
        except Exception:
            pass


def attach(api) -> bool:
    """Hand the lifecycle to the host, by whichever name this build answers to.

    These two helpers are public names, not `_private`: the gate refuses a skin
    that *names* anything with a leading underscore, because that is how code
    reaches for `__globals__`, and a reference pack that tripped its own gate
    would be a bad first lesson.
    """
    given = hooks()
    for verb in ("skin_hooks", "register_skin", "lifecycle", "hook"):
        call = getattr(api, verb, None)
        if callable(call):
            try:
                call(NAME, given)
                return True
            except Exception:
                continue
    try:
        import beeagent.core.skins as engine   # the full name, as the gate reads it
    except Exception:
        return False
    for verb in ("register", "register_skin", "attach"):
        call = getattr(engine, verb, None)
        if callable(call):
            try:
                call(NAME, given)
                return True
            except Exception:
                continue
    return False
