"""`skin-pulse` — the animated reference: everything on screen moves because the
model is working, and every movement traces back to one of the agent's events.

What it demonstrates about the engine:

*  a **thinking pulse** whose phase is advanced by `dt` and nudged by each token
   (`reasoning_delta` while the model reasons, `stream_delta` while it writes),
   so the speed of the light is the model's speed, not a clock's;
*  a **tool progress line** assembled from `tool_start` / `tool_end`, drawn from
   the skin's own character ramp: the ramp is one string and every frame of every
   animation is an index into it. There is no list of spinner states anywhere in
   this file, and that is the point — a stranger adding an animation should see
   that they need characters, not assets;
*  **honest degradation**: a terminal that cannot do colour gets the static line
   the baseline skin draws, and a skin that spends more than the host's frame
   budget demotes itself before the host has to notice.

Frames are deterministic. Nothing reads the wall clock to decide *what* to draw —
`dt` is the only time a phase depends on, so a test can advance a whole turn with
`on_frame(0.05, painter)` and assert the phase it should be in. `time` is used for
one thing only: measuring how long this frame took.
"""
import itertools
import math
import string
import time
from collections import deque

# --- brand, kept: honey and leaf, and not one cyan pixel -----------------------
# The shimmer ramp from beeagent/ui/components.py, reused rather than re-invented:
# pale honey -> gold -> leaf. The pulse walks this list forwards and backwards.
SHIMMER = ["#fff9c4", "#ffee58", "#ffcc00", "#f6a821", "#c0ca33", "#7cb342", "#43a047"]
HONEY = "#ffcc00"
LEAF = "#7cb342"
DEEP_LEAF = "#43a047"
AMBER = "#ff8a00"           # the logo's own "not the usual path"

# The same stops where the terminal knows only sixteen names. A collapse, not a
# substitution: the ramp keeps walking, it just has two places to stand instead
# of seven. Anything not listed is refused below rather than passed through.
ANSI16 = {stop: ("yellow" if stop in SHIMMER[:4] else "green") for stop in SHIMMER}
ANSI16[HONEY] = "yellow"
ANSI16[LEAF] = "green"
ANSI16[DEEP_LEAF] = "green"
ANSI16[AMBER] = "red"

# One cycle of the ramp, dark to loud. Every animated glyph in this skin is an
# index into this string. ASCII throughout: a cp1251 console has no braille
# characters, and the machines this project ships to include a lot of them.
RAMP = ".-~=+*#%@"
FILL = "#"                  # the "already done" cell of the progress line

PRINTABLE = frozenset(string.printable) - frozenset("\r\n\t\x0b\x0c")

# The two-language word list, read once per frame rather than rebuilt. `L(en, ru)`
# is the engine's own shape; a pack may not import `beeagent.i18n` (not on the
# allow-list), so `say()` below applies the same rule against these constants.
WORDS = {
    "ready": ("ready", "готов"),
    "idle": ("idle", "в покое"),
    "thinking": ("thinking", "думаю"),
    "writing": ("writing", "пишу"),
    "running": ("running", "выполняю"),
    "ran": ("ran", "выполнил"),
    "refused": ("refused", "отказ"),
    "cache": ("from cache", "из кэша"),
    "retry": ("retrying", "повторяю"),
    "trimmed": ("trimmed", "урезан"),
    "stopped": ("stopped", "остановлено"),
    "failed": ("failed", "ошибка"),
}
# The state machine's name -> which word to show for it.
_LABEL = {"idle": "ready", "thinking": "thinking", "writing": "writing",
          "tool": "running", "finished": "ran", "denied": "refused",
          "cache": "cache", "retry": "retry", "trimmed": "trimmed",
          "stopped": "stopped", "failed": "failed"}

NAME = "pulse"
_WATCHED = ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
            "tool_denied", "done", "stopped", "error", "retry", "economy_hit",
            "context_trimmed", "nudged")

# --- timing, in seconds of the dt the host hands us -----------------------------
PULSE_PERIOD = 0.85         # one breath of the thinking light
TOKEN_EVERY = 4             # tokens that nudge the ramp one cell
SPAN = 6.0                  # a tool still running past this has no bar left to fill
HOLD = 1.2                  # ...and a finished tool stays on the line this long
FRAME_MIN = 1.0 / 30.0      # never repaint faster than this, whatever dt says
BUDGET_US = 8000            # the host demotes above ~8 ms; hold under half of it
STUCK_FOR = 3               # ...after this many frames of overshooting


# --- helpers every skin in this repository repeats -------------------------------
# Repeated rather than shared: a pack has to survive being copied out on its own,
# and the host's import allow-list has no "skin helpers" module in it.

def depth_of(support) -> int:
    """`color_support()` as one number: 0 plain, 1 sixteen, 2 two-fifty-six, 3 millions.

    Defensive because the answer is build-specific and a skin must not fall over
    on a spelling it has not seen: int, bool, string and None are all answers a
    real painter has been known to give.
    """
    if support is None:
        return 0
    # A bool is Python's way of spelling "no colour at all": `False` is plain,
    # `True` is the least we can call colour. Reading `False` as `int` 1 below
    # would hand a colourless terminal sixteen names it cannot print.
    if isinstance(support, bool):
        return 1 if support else 0
    if isinstance(support, int):
        if support >= 0x10000:
            return 3
        return 2 if support >= 256 else (1 if support >= 8 else 0)
    text = str(support).strip().lower()
    if text in ("", "0", "none", "mono", "plain", "no", "false", "1-bit"):
        return 0
    if "truecolor" in text or "24-" in text or "24 bit" in text or text in ("rgb", "full"):
        return 3
    if "256" in text or "8-" in text:
        return 2
    return 1


def tone(value, depth: int):
    """A colour in the spelling this terminal can print — None for plain text.

    Never returns a hex on a terminal that cannot take one: an escape a console
    does not understand is printed as letters, which is worse than no colour.
    """
    if depth <= 0 or value is None:
        return None
    if depth >= 2:
        return value
    return ANSI16.get(value, "white")


def ask(painter, name: str, default=None):
    """`painter.<name>()` when this build of the painter has it, else `default`."""
    call = getattr(painter, name, None)
    if call is None:
        return default
    try:
        return call()
    except Exception:
        return default


def size_of(painter) -> tuple:
    """(cols, rows), floored at something paintable."""
    cols, rows = ask(painter, "get_terminal_size", (80, 24)) or (80, 24)
    try:
        return max(int(cols), 20), max(int(rows), 4)
    except (TypeError, ValueError):
        return 80, 24


def clean(text, limit: int = 26) -> str:
    """Event payload as screen ink: printable ASCII, one line, bounded.

    Tool names and arguments are text a model wrote. Without the strip, an
    argument carrying an escape sequence would land in the status line and do
    whatever that sequence does to the terminal.
    """
    kept = [ch for ch in str(text) if ch in PRINTABLE][:limit]
    return "".join(kept).strip()


# --- the skin ---------------------------------------------------------------------

class Pulse:
    """One row of light at the bottom of the screen, and the numbers behind it."""

    __slots__ = (
        "cols", "rows", "depth", "lang", "translate",
        "clock", "phase", "scroll", "since", "tokens", "thoughts", "answer",
        "state", "running", "finished", "lines", "denials", "cache_hits",
        "dirty", "last_drawn", "bar_cache", "line_cache",
        "slow", "demoted", "spent_us", "worst_us",
    )

    def __init__(self):
        self.cols, self.rows = 80, 24
        self.depth = None
        self.lang = "en"
        self.translate = None
        # Time we are *given*. Every phase below is a pure function of it.
        self.clock = 0.0
        self.phase = 0.0        # 0..1 through one breath
        self.scroll = 0         # index into RAMP, wrapped: never grows
        self.since = 0.0        # time since the last repaint, for the throttle
        self.tokens = 0         # the per-token tick
        self.thoughts = 0
        self.answer = 0
        self.state = "idle"
        # Bounded by construction: four tools at once is already more than the
        # agent runs, and a finished tool is dropped once HOLD has passed.
        self.running = deque(maxlen=4)      # (tool, clock)
        self.finished = deque(maxlen=4)     # (tool, clock, error)
        self.lines = 0
        self.denials = 0
        self.cache_hits = 0
        self.dirty = True
        self.last_drawn = ""
        # One-entry caches, not dicts: the same bar repeats frame after frame and
        # a cache that outlives the turn is a memory leak with a friendly name.
        self.bar_cache = None
        self.line_cache = None
        self.slow = 0
        self.demoted = False
        self.spent_us = 0.0
        self.worst_us = 0.0

    # -- language ----------------------------------------------------------------

    def say(self, english: str, russian: str) -> str:
        """Two languages, one call.

        `beeagent.i18n` is not on the host's allow-list, so a skin may not import
        `L`. The translator arrives on the context when the engine has one to
        hand out; until then this is the same rule with the same default.
        """
        if self.translate is not None:
            try:
                return self.translate(english, russian)
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    # -- the animation, as numbers -----------------------------------------------

    def wave(self) -> float:
        """0..1..0 across a breath; the cosine makes it a pulse, not a sawtooth."""
        return 0.5 - 0.5 * math.cos(2 * math.pi * self.phase)

    def pulse_stop(self) -> int:
        """Which rung of the shimmer ramp this frame sits on."""
        return int(self.wave() * (len(SHIMMER) - 1))

    def ramp_cell(self) -> str:
        """The single character the pulse is on. Every glyph here is from RAMP."""
        return RAMP[int(self.wave() * (len(RAMP) - 1))]

    def bar(self, width: int) -> str:
        """The flowing thinking bar: a window onto the endless ramp, slid by scroll."""
        width = max(1, min(width, self.cols))
        key = (self.scroll, width)
        if self.bar_cache is not None and self.bar_cache[0] == key:
            return self.bar_cache[1]
        # `scroll` is wrapped in `tick`, so this walks at most len(RAMP)+width
        # characters: the cost of the line does not grow with the answer.
        text = "".join(itertools.islice(itertools.cycle(RAMP), self.scroll,
                                        self.scroll + width))
        self.bar_cache = (key, text)
        return text

    def progress(self) -> float:
        """How full the running tool's bar is, 0..1. The newest call wins."""
        if not self.running:
            return 0.0
        started = self.running[-1][1]
        return max(0.0, min(1.0, (self.clock - started) / SPAN))

    def progress_bar(self, width: int) -> str:
        """A tool bar whose bright leading cell is the same ramp the pulse uses."""
        width = max(3, min(width, self.cols))
        edge = int(self.wave() * (len(RAMP) - 1))
        filled = max(1, int(width * self.progress()))
        key = (edge, filled, width)
        if self.line_cache is not None and self.line_cache[0] == key:
            return self.line_cache[1]
        text = (FILL * max(filled - 1, 0) + RAMP[edge]
                + " " * max(width - filled, 0))
        self.line_cache = (key, text)
        return text

    # -- time ---------------------------------------------------------------------

    def advance(self, dt) -> None:
        """Move the virtual clock. Draws nothing, so a throttled frame is free."""
        try:
            step = float(dt)
        except (TypeError, ValueError):
            step = 0.0
        # A NaN handed to `advance` would poison every phase and could not be
        # un-poisoned, so it never gets in.
        if not math.isfinite(step) or step < 0:
            step = 0.0
        self.clock += step
        self.phase = (self.phase + step / PULSE_PERIOD) % 1.0
        self.since += step
        # A finished tool leaves the line by itself: no timer, no thread, nothing
        # that can outlive the turn it was describing.
        while self.finished and self.clock - self.finished[-1][1] > HOLD:
            self.finished.pop()
            if self.state == "finished":
                self.state = "idle"
                self.dirty = True

    def tick(self, count: int = 1) -> None:
        """The per-token tick: every `TOKEN_EVERY` tokens slides the ramp one cell.

        Counting each token straight into the scroll would repaint the line per
        token and make the bar's speed a function of the provider's tokenizer.
        Four to a cell reads the same and moves at a rate the eye follows.
        """
        self.tokens += count
        if self.tokens % TOKEN_EVERY == 0:
            self.scroll = (self.scroll + 1) % len(RAMP)

    # -- events: state only, never paint -------------------------------------------

    def on_event(self, event: str, payload) -> None:
        data = payload if isinstance(payload, dict) else {}
        if event == "reasoning_delta":
            self.thoughts += 1
            self.tick()
            self._set("thinking")
        elif event == "stream_delta":
            self.answer += 1
            self.tick()
            self._set("writing")
        elif event == "tool_start":
            self.running.append((clean(data.get("tool", "?"), 20), self.clock))
            self._set("tool")
        elif event == "tool_end":
            tool = clean(data.get("tool", "?"), 20)
            if self.running:
                self.running.popleft()
            self.finished.append((tool, self.clock, bool(data.get("error"))))
            self._set("finished")
        elif event == "tool_denied":
            self.denials += 1
            self.running.clear()
            self._set("denied")
        elif event == "economy_hit":
            self.cache_hits += 1
            self._set("cache")
        elif event == "retry":
            self._set("retry")
        elif event == "context_trimmed":
            self._set("trimmed")
        elif event == "nudged":
            self._set("thinking")
        elif event == "done":
            self.running.clear()
            self._set("idle")
        elif event == "stopped":
            self.running.clear()
            self._set("stopped")
        elif event == "error":
            self.running.clear()
            self._set("failed")

    def on_output(self, text) -> None:
        """Answer blocks: counted, never kept. Only whether the model is writing
        matters to this line, and a copy of the answer would be the leak."""
        if text:
            self.lines += 1
            self._set("writing")

    def _set(self, state: str) -> None:
        self.state = state
        self.dirty = True

    # -- drawing --------------------------------------------------------------------

    def label(self) -> str:
        """The word the line starts with, in the active language.

        Reads the module's word table rather than rebuilding a dict and calling
        `say` for every state each frame: this is on the per-paint path, and a
        reference skin that costs a dozen throwaway tuples a frame is teaching the
        next person the wrong lesson about what a frame should spend.
        """
        return self.say(*WORDS[_LABEL.get(self.state, "ready")])

    def counter(self) -> str:
        """Tokens and their rate, off the virtual clock: the same dt sequence
        gives the same number in a test and in a terminal."""
        span = self.clock if self.clock > 0.001 else 0.001
        return "%dt %.0f/s" % (self.tokens, self.tokens / span)

    def compose(self, cols: int) -> tuple:
        """This frame's line as (text, colour). The colour is a hex from the
        palette; `tone()` decides what the terminal actually gets."""
        head = self.label()
        if self.state in ("tool", "finished"):
            name = ""
            failed = False
            if self.running:
                name = self.running[-1][0]
            elif self.finished:
                name, _at, failed = self.finished[-1]
            room = max(3, min(18, cols - len(head) - len(name) - 8))
            return ("%s %s [%s]" % (head, name, self.progress_bar(room)),
                    AMBER if failed else LEAF)
        room = max(1, min(24, cols - len(head) - len(self.counter()) - 3))
        return ("%s %s %s" % (head, self.bar(room), self.counter()),
                SHIMMER[self.pulse_stop()])

    def static_line(self) -> str:
        """What the line says when nothing is animating — the baseline's shape:
        brand, marker, a word. Deliberately no counter: a static line is what a
        terminal that gets *one* repaint, or a skin that spent its budget, shows,
        and a rate belongs to the animation, not to the thing you fall back to.
        """
        word = self.label() if self.state != "idle" else self.say("idle", "в покое")
        return "BeeCode  o  %s" % word

    def on_frame(self, dt, painter) -> None:
        """Advance, then maybe paint.

        The two are separate on purpose: the numbers stay exact at any frame
        rate — the clock always advances — while the screen is written at most 30
        times a second on an animated terminal, and only when the word on it
        changes on a plain or a demoted one. A stream of tokens lands here far
        more often than either, and must never cost a repaint apiece.
        """
        if self.depth is None:
            self.depth = depth_of(ask(painter, "color_support", None))
        self.advance(dt)
        if self.demoted or self.depth <= 0:
            # No colour, or no budget: one static line, and only when it changed.
            line = self.static_line()[:self.cols]
            if self.dirty or line != self.last_drawn:
                self._draw(painter, line, None)
                self.dirty = False
            return
        if self.since < FRAME_MIN:
            return                              # the cheap path: two float adds
        line, value = self.compose(self.cols)
        self._paint(painter, line[:self.cols], tone(value, self.depth))

    def _draw(self, painter, text: str, colour) -> None:
        """The one place a pixel moves: clear the row, lay the text, log it.

        Separate from `_paint` so a frame that turns out to be over budget can
        put the fallback back over its own ink without re-timing itself.
        """
        cols, rows = size_of(painter)
        self.cols, self.rows = cols, rows
        y = rows - 1
        painter.clear_region(0, y, cols, 1)
        painter.draw_text(0, y, text, color=colour)
        self.last_drawn = text
        self.since = 0.0

    def _paint(self, painter, text: str, colour) -> None:
        start = time.perf_counter_ns()
        self._draw(painter, text, colour)
        self.dirty = False
        spent = (time.perf_counter_ns() - start) / 1000.0
        self.spent_us = spent
        if spent > self.worst_us:
            self.worst_us = spent
        # Self-policing: the host demotes a skin over its budget, and a skin that
        # waits to be told has already cost the user every frame up to this one.
        if spent > BUDGET_US:
            self.slow += 1
            if self.slow >= STUCK_FOR:
                self.demoted = True
                # The frame that tipped us over is the last one that animates:
                # lay the static line back over its ink, so we fall back, not out.
                self._draw(painter, self.static_line()[:self.cols], None)
        else:
            self.slow = 0


# --- module surface the host reads -----------------------------------------------

skin = Pulse()


def on_init(ctx) -> None:
    skin.lang = clean(getattr(ctx, "language", "en") or "en", 8) or "en"
    give = getattr(ctx, "translate", None) or getattr(ctx, "L", None)
    skin.translate = give if callable(give) else None
    painter = getattr(ctx, "painter", None)
    if painter is not None:
        skin.depth = depth_of(ask(painter, "color_support", None))
        skin.cols, skin.rows = size_of(painter)


def on_frame(dt, painter) -> None:
    skin.on_frame(dt, painter)


def on_event(event, payload) -> None:
    skin.on_event(event, payload)


def on_output(text) -> None:
    skin.on_output(text)


def hooks() -> dict:
    """This skin's lifecycle, by name — the thing `setup` hands to the host."""
    return {"on_init": on_init, "on_frame": on_frame,
            "on_event": on_event, "on_output": on_output}


# --- registration ------------------------------------------------------------------
#
# `setup(api)` is the loader's own entry point and runs today. The verbs for
# handing over lifecycle hooks are new, so the name of the right one is still
# moving: try the plausible spellings, then the engine module itself, then fall
# back to the event bus the current host already has. A pulse that cannot
# register still tracks state; a pulse that raises takes the interface down.

def setup(api) -> None:
    if not _attach(api):
        for name in _WATCHED:
            api.event(name, on_event)
    _contribute(api)


def _contribute(api) -> None:
    """Say so in `/extensions`, whichever route took the hooks.

    A skin is something the user can see and choose, so it files itself through
    the documented `api.skin` verb, which records a "skin" contribution under this
    plugin's name. Guarded, because the stand-in hosts in the tests hand over
    lifecycle hooks or an event bus and have no `skin` at all — and a pack must
    never raise inside `setup`, which the loader would report as a load error.
    """
    give = getattr(api, "skin", None)
    if callable(give):
        try:
            give("skins", NAME, hooks())
        except Exception:
            pass


def _attach(api) -> bool:
    given = hooks()
    for verb in ("skin_hooks", "register_skin", "lifecycle", "hook"):
        call = getattr(api, verb, None)
        if callable(call):
            try:
                call(NAME, given)
                return True
            except TypeError:
                continue                     # right verb, different shape: try on
            except Exception:
                continue
    try:                                     # the engine itself, imported lazily
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
