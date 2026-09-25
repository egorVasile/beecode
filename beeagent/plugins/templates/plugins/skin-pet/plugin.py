"""`skin-pet` — the limit case: a creature in the corner whose whole job is to
have a feeling about what the agent is doing.

Why this one exists next to `skin-pulse`: the pulse shows that the engine can
drive an animation from events. The pet shows the other half — that a skin may
own a little world, with a clock of its own, art of its own, and a memory that
stays the same size however long the session runs.

The rules it is built to, which are the rules the host enforces:

*  **It animates from `on_frame(dt)` and nowhere else.** Events move a mood and
   drop a timestamp; no event handler draws. That is what keeps a burst of five
   hundred tokens from producing five hundred repaints, and it is why every frame
   here is a function of the `dt` the host chose to hand us — deterministic, with
   no sleeping, no thread, no timer.
*  **Its state is `__slots__` and one bounded deque.** `context_trimmed` after
   four hours cannot make the skin heavier than it was on the first frame.
*  **It watches its own frame cost** and falls back to the baseline's static line
   after a few frames over the host's ~8 ms budget, so being ambitious is not the
   same as being expensive.
*  **ASCII art, five columns wide, one row at a time.** No box drawing, no
   braille, no emoji: a cp1251 console — the ordinary Russian Windows console —
   prints none of those, and the pet would turn into `?????` at exactly the
   moment it was supposed to be cheering.

Moods: idle (which blinks), thinking (a bob and a thought bubble), a hop when the
answer lands, hiding and shivering after an error or a refused tool, and chasing
a ball across the bottom when the cache answered instead of the model.
"""
import math
import random
import string
import time
from collections import deque

# --- brand: honey and leaf, with somewhere to put an alarm ----------------------
HONEY = "#ffcc00"
PALE_HONEY = "#ffd54f"
LEAF = "#7cb342"
DEEP_LEAF = "#43a047"
NIGHT = "#08170a"          # the dark green the built-in interface already sits on
AMBER = "#ff8a00"          # the logo's own "not the usual path"

# Where a terminal only knows sixteen names. A collapse, not a substitution.
ANSI16 = {HONEY: "yellow", PALE_HONEY: "yellow", LEAF: "green", DEEP_LEAF: "green",
          NIGHT: "black", AMBER: "red"}

# The pet, as text. Every pose is exactly POSE_H rows of exactly POSE_W characters
# — a test checks that, because a pose of another width would tear the box.
POSE_W, POSE_H = 5, 3
ART = {
    "idle":    ((" v v ", "(o.o)", "(_|_)"),),
    "blink":   ((" v v ", "(-.-)", "(_|_)"),),
    "think":   ((" v v ", "(o.o)", "(_|_)"), (" v v ", "(o.o)", "(_|-)"),
                (" v v ", "(o.o)", "(-|_)")),
    "write":   ((" v v ", "(o.o)", "(_|_)"), (" v v ", "(o.o)", "(_|=)")),
    "work":    ((" v v ", "(>-<)", "(_|_)"), (" v v ", "(O.O)", "(_|_)")),
    "hop":     ((" v v ", "(^w^)", "(_|_)"), ("     ", "(^w^)", "(_|_)")),
    "hide":    (("     ", " (  )", "     "), ("     ", "(-.-)", "     ")),
    "chase":   ((" v v ", "(o.o)", "(_|_)"), (" v v ", "(o.o)", " |_|_")),
    "worry":   ((" v v ", "(;.;)", "(_|_)"),),
    "startle": ((" v v ", "(O.O)", "(_|_)"),),
    "sit":     ((" v v ", "(o.o)", "(___)"),),
    "shed":    (("  v  ", "(o.o)", "(_|_)"),),
}

# Pose changes per second, per mood. A mood missing here holds its one pose and
# moves for other reasons (the bob, the shiver, the ball).
RATE = {"think": 2.5, "write": 3.0, "work": 5.0, "hop": 6.0, "chase": 6.0,
        "startle": 1.5, "shed": 1.5, "worry": 2.0, "hide": 1.2}

# Moods that end by themselves, in seconds of the dt we are handed. `work` is not
# among them: a tool in progress is a fact, not a mood, and an idle pet while the
# machine is running would be the pet lying.
TRANSIENT = {"hop": 1.6, "hide": 2.8, "chase": 2.4, "startle": 1.2, "worry": 2.0,
             "shed": 1.8, "sit": 4.0}

BLINK_EVERY = (3.2, 6.5)   # seconds, jittered by the seeded rng below
BLINK_HELD = 0.14
BOB_PERIOD = 1.1           # thinking: one up-and-down per this
HOP_HEIGHT = 2             # rows the pet leaves the ground for, out of its box
SHIVER_HZ = 14.0           # how fast a frightened pet rattles
BALL_HZ = 9.0              # columns a second the cache ball crosses at
FRAME_MIN = 1.0 / 12.0     # a pet this small is quite enough at twelve frames a second
BUDGET_US = 8000           # the host demotes above ~8 ms
STUCK_FOR = 3

# Seeded, so a run of frames gives the same "random" blink every time. The point
# of a pet is that it looks alive; the point of a *testable* one is that the
# liveliness repeats when a test asks for it.
_rng = random.Random(0x5EED)

PRINTABLE = frozenset(string.printable) - frozenset("\r\n\t\x0b\x0c")

NAME = "pet"
_WATCHED = ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
            "tool_denied", "done", "stopped", "error", "retry", "economy_hit",
            "context_trimmed", "nudged")

# Event -> mood. The whole personality of this skin is this table plus `on_frame`:
# nothing below decides anything at the moment an event arrives.
_MOOD_FOR = {"reasoning_delta": "think", "stream_delta": "write",
             "tool_start": "work", "nudged": "startle", "retry": "worry",
             "context_trimmed": "shed", "stopped": "sit", "error": "hide",
             "tool_denied": "hide", "done": "hop", "economy_hit": "chase"}

# What each mood is called, in two languages — the engine's `L(en, ru)` shape. A
# pack may not import `beeagent.i18n` (it is not on the allow-list), so `say()`
# applies the same rule against this table. It lives at module level so the title,
# which names the last few moods, reads it instead of rebuilding a dozen tuples a
# frame: `mood_word` is on the per-paint path, up to four times a frame.
WORDS = {
    "idle": ("idle", "покоится"),
    "blink": ("blinking", "моргает"),
    "think": ("thinking", "думает"),
    "write": ("writing", "пишет"),
    "work": ("working", "работает"),
    "hop": ("happy", "рад"),
    "hide": ("hiding", "прячется"),
    "chase": ("chasing", "ловит"),
    "worry": ("nervous", "нервничает"),
    "startle": ("startled", "вскочил"),
    "sit": ("sitting", "сидит"),
    "shed": ("shedding", "линяет"),
    "pet": ("pet", "питомец"),
}


# --- helpers every skin in this repository repeats -------------------------------
# Repeated rather than shared: a pack has to survive being copied into a project on
# its own, and the host's import allow-list has no common skin module in it.

def depth_of(support) -> int:
    """`color_support()` as one number: 0 plain, 1 sixteen, 2 two-fifty-six, 3 millions."""
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

    Never hands a hex to a console that would print the hex as letters.
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


def clean(text, limit: int = 20) -> str:
    """Event payload as screen ink: printable ASCII, one line, bounded."""
    kept = [ch for ch in str(text) if ch in PRINTABLE][:limit]
    return "".join(kept).strip()


# --- the pet -----------------------------------------------------------------------

class Pet:
    """Mood, clock, and a memory that cannot grow."""

    __slots__ = (
        "cols", "rows", "depth", "lang", "translate",
        "clock", "mood", "mood_since", "since", "bob", "shiver", "ball",
        "next_blink", "blink_until", "moods", "tools", "runs", "denials",
        "hops", "chases", "lines", "dirty", "title_cache", "shown",
        "slow", "demoted", "spent_us", "worst_us",
    )

    def __init__(self):
        self.cols, self.rows = 80, 24
        self.depth = None
        self.lang = "en"
        self.translate = None
        self.clock = 0.0
        self.mood = "idle"
        self.mood_since = 0.0
        self.since = 0.0            # for the repaint throttle
        self.bob = 0                # rows off the ground, negative is up
        self.shiver = 0             # columns, -1/0/+1
        self.ball = -1              # x of the cache ball; -1 means no ball
        self.next_blink = _rng.uniform(*BLINK_EVERY)
        self.blink_until = 0.0
        # The last few moods, for the box title. Bounded by maxlen: four hours of
        # events cannot make the pet heavier than it is on the first frame.
        self.moods = deque(maxlen=4)
        self.tools = 0
        self.runs = 0
        self.denials = 0
        self.hops = 0
        self.chases = 0
        self.lines = 0
        self.dirty = True
        self.title_cache = None
        self.shown = ""
        self.slow = 0
        self.demoted = False
        self.spent_us = 0.0
        self.worst_us = 0.0

    # -- language --------------------------------------------------------------------

    def say(self, english: str, russian: str) -> str:
        """`beeagent.i18n.L` is not on the host's allow-list, so a skin may not
        import it. The translator arrives on the context when the engine has one to
        hand out; this is the same rule, with the same default, locally."""
        if self.translate is not None:
            try:
                return self.translate(english, russian)
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    def mood_word(self, mood: str = "") -> str:
        """What the pet is doing, in the active language. Reads the module table."""
        return self.say(*WORDS.get(mood or self.mood, WORDS["idle"]))

    # -- state moves in on_event, and nothing else -------------------------------------

    def on_event(self, event: str, payload) -> None:
        """Bookkeeping only. An event that painted would repaint once per token."""
        data = payload if isinstance(payload, dict) else {}
        if event == "tool_start":
            self.tools += 1
        elif event == "tool_end":
            self.runs += 1
        elif event == "tool_denied":
            self.denials += 1
        elif event == "done":
            self.hops += 1
        elif event == "economy_hit":
            self.chases += 1
        mood = _MOOD_FOR.get(event)
        if event == "tool_end":
            # A failed tool is the same face as a refusal; the difference is in the
            # line of text the host is already printing, not in the pet.
            mood = "hide" if data.get("error") else "write"
        # A hop is not interrupted by a token: let it land, then read the answer.
        if mood == "write" and self.mood == "hop" and self.age() < TRANSIENT["hop"]:
            mood = None
        if mood:
            self._mood(mood)

    def on_output(self, text) -> None:
        """Counted, not kept. The pet has opinions about the answer, not a copy of
        it — holding the text here would be the unbounded history, in the one hook
        that hands over the most bytes."""
        if text:
            self.lines += 1
            self._mood("write")

    def _mood(self, mood: str) -> None:
        if mood == self.mood and mood not in TRANSIENT:
            return
        self.mood = mood
        self.mood_since = self.clock
        self.moods.append(mood)
        self.dirty = True

    # -- the clock ----------------------------------------------------------------------

    def age(self) -> float:
        """Seconds since the current mood started, on the clock we were given."""
        return self.clock - self.mood_since

    def advance(self, dt) -> None:
        """The only place time goes forward, and the only place the pet decides
        anything about how it looks."""
        try:
            step = float(dt)
        except (TypeError, ValueError):
            step = 0.0
        # A NaN handed to `advance` would poison every phase in the skin, and a
        # float cannot be un-poisoned: check it at the door.
        if not math.isfinite(step) or step < 0:
            step = 0.0
        self.clock += step
        self.since += step
        limit = TRANSIENT.get(self.mood)
        if limit is not None and self.age() >= limit:
            self._mood("idle")
        self._blink()
        self._motion()

    def _blink(self) -> None:
        """Idle is never quite still: every few seconds the pet shuts its eyes."""
        if self.clock >= self.next_blink:
            self.next_blink = self.clock + _rng.uniform(*BLINK_EVERY)
            if self.mood == "idle":
                self.blink_until = self.clock + BLINK_HELD

    def _motion(self) -> None:
        """Bob, shiver, ball — three pure functions of the clock, so a test can
        advance dt and read back the pose it expects instead of a screenshot."""
        self.bob = 0
        self.shiver = 0
        self.ball = -1
        mood = self.mood
        if mood == "think":
            self.bob = int(round(math.sin(2 * math.pi * self.clock / BOB_PERIOD)))
        elif mood == "hop":
            # Up and back down over TRANSIENT["hop"], the peak in the middle. The
            # hop is taller than the box, and the box is cleared two rows up for
            # exactly that reason: a pet that cannot leave its box is a sprite.
            span = TRANSIENT["hop"]
            self.bob = -int(round(HOP_HEIGHT * math.sin(math.pi * self.age() / span)))
        elif mood == "hide":
            self.shiver = 1 if int(self.clock * SHIVER_HZ) % 2 else -1
        elif mood == "chase":
            room = max(self.cols - POSE_W - 4, POSE_W)
            self.ball = int(self.age() * BALL_HZ) % (room + 2)

    def pose_index(self, frames: int) -> int:
        """Which of this mood's poses is on screen now."""
        if frames < 2:
            return 0
        return int(self.clock * RATE.get(self.mood, 0.0)) % frames

    # -- drawing -------------------------------------------------------------------------

    def sprite(self) -> tuple:
        """The rows of the pet for this frame."""
        if self.mood == "idle" and self.clock < self.blink_until:
            return ART["blink"][0]
        frames = ART.get(self.mood) or ART["idle"]
        return frames[self.pose_index(len(frames))]

    def title(self) -> str:
        """The box title: the pet, and the last four things it felt.

        Cached on the mood list, because it only changes when a mood does and this
        is on the per-frame path.
        """
        key = ",".join(self.moods) or self.mood
        if self.title_cache is not None and self.title_cache[0] == key:
            return self.title_cache[1]
        joined = "<".join(self.mood_word(m) for m in self.moods) or self.mood_word()
        text = "%s %s" % (self.say("pet", "питомец"), joined)
        self.title_cache = (key, text)
        return text

    def geometry(self) -> tuple:
        """(x, y, w, h) of the pet's corner, kept inside whatever screen there is."""
        w = POSE_W + 4
        h = POSE_H + 2
        x = 1
        y = max(self.rows - h - 2, 1)
        return x, y, w, h

    def on_frame(self, dt, painter) -> None:
        """Advance the world, then draw it — twelve times a second at most, and not
        at all once the skin has demoted itself."""
        if self.depth is None:
            self.depth = depth_of(ask(painter, "color_support", None))
        self.advance(dt)
        if self.depth <= 0 or self.demoted:
            self._paint_plain(painter)
            return
        if self.since < FRAME_MIN:
            return                        # the cheap path: two float adds and out
        self._paint(painter)

    def _paint(self, painter) -> None:
        start = time.perf_counter_ns()
        cols, rows = size_of(painter)
        self.cols, self.rows = cols, rows
        depth = self.depth
        x, y, w, h = self.geometry()
        # Two rows above the box are cleared as well: the hop goes there.
        painter.clear_region(max(x - 1, 0), max(y - 1, 0), w + 2, h + 1)
        if rows >= 8:
            painter.draw_box(x, y, w, h, border=tone(LEAF, depth),
                             fill=tone(NIGHT, depth) if depth >= 2 else None,
                             title=self.title()[:max(w - 2, 6)])
        body = tone(HONEY, depth) if self.mood not in ("hide", "worry") else tone(AMBER, depth)
        for row, line in enumerate(self.sprite()):
            painter.draw_text(x + 2 + self.shiver, y + 1 + row + self.bob, line, color=body)
        if self.ball >= 0:
            painter.draw_text(x + 1 + self.ball, y + 2, "o", color=tone(PALE_HONEY, depth))
        if self.mood == "think":
            # A second animation in one line, from the same idea as skin-pulse:
            # characters in a string, indexed by the clock.
            bubble = ".:*"
            painter.draw_text(x + w - 2, y, bubble[int(self.clock * 3) % len(bubble)],
                              color=tone(DEEP_LEAF, depth))
        self.shown = self.mood
        self.dirty = False
        self.since = 0.0
        spent = (time.perf_counter_ns() - start) / 1000.0
        self.spent_us = spent
        if spent > self.worst_us:
            self.worst_us = spent
        # Self-policing, as in skin-pulse: the host demotes a skin over its budget,
        # and one that waits to be told has already cost the user every frame to
        # here. A pet that is dropped is still on screen, as one honest word.
        if spent > BUDGET_US:
            self.slow += 1
            if self.slow >= STUCK_FOR:
                self.demoted = True
        else:
            self.slow = 0

    def _paint_plain(self, painter) -> None:
        """No colour, no budget: the pet says one word and stops moving.

        This is the baseline skin's line, because a degraded skin should look like
        the thing everybody agrees is readable — not like a broken animation.
        """
        cols, rows = size_of(painter)
        self.cols, self.rows = cols, rows
        text = "BeeCode  o  %s" % self.mood_word()
        if text == self.shown:
            self.dirty = False
            return
        y = rows - 1
        painter.clear_region(0, y, cols, 1)
        painter.draw_text(0, y, text[:cols], color=None)
        self.shown = text
        self.dirty = False
        self.since = 0.0


# --- module surface the host reads ---------------------------------------------------

skin = Pet()


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
    """This skin's lifecycle, by name — what `setup` hands to the host."""
    return {"on_init": on_init, "on_frame": on_frame,
            "on_event": on_event, "on_output": on_output}


# --- registration -----------------------------------------------------------------------
#
# Same story in all three packs: `setup(api)` is the entry point that runs today,
# and the verb that accepts lifecycle hooks is new enough that its name is still a
# guess. So: try the plausible spellings, then the engine module itself (imported
# lazily, because it may not be on disk yet), then the event bus the current host
# does have. A skin that cannot register still tracks state; a skin that raises
# takes the interface down with it.

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
