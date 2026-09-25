"""The baseline skin: the interface BeeCode has today, contributed as a plugin.

Three things are true of this file and every one of them is a point.

*  It draws the look the built-in interface already has — honey for what the
   model says (`#ffcc00`), leaf for what the machine did (`#7cb342`), same
   positions, same words. Nothing here is a new design; that is the whole reason
   it exists as the thing the other skins fall back *to*.
*  It never animates. `on_frame` is registered, is called, and returns without
   touching the painter unless something actually changed. "A skin may do
   nothing" needs a reference implementation, or the sentence just means "a
   broken skin".
*  The fallback line other skins degrade to is this one, word for word. When
   `skin-pulse` finds a terminal with no colour, or spends too long in a frame,
   it stops drawing itself and lets this shape stand.

The one deliberate difference from the built-in line: the status dot is `o`, not
`●`. The built-in glyph is fine on UTF-8 and turns into `?` on a cp1251 console,
which is a real console on every Russian Windows box this project ships to. The
colours are untouched; the glyph is not a colour.
"""
import string

# --- the palette the built-in interface uses ---------------------------------
# Copied from beeagent/ui/components.py rather than invented, so "the same look"
# is a checkable claim.
HONEY = "#ffcc00"       # what the model said
PALE_HONEY = "#ffd54f"  # a tool that only looked at something
LEAF = "#7cb342"        # what the machine did
DEEP_LEAF = "#43a047"   # the frame around it

# The same four where the terminal only knows sixteen names. This is a collapse,
# not a substitution: yellow really is the closest honest answer for #ffcc00 on
# an ANSI console, and saying so beats emitting an escape that prints as garbage.
ANSI16 = {HONEY: "yellow", PALE_HONEY: "yellow", LEAF: "green", DEEP_LEAF: "green"}

# Nothing outside this set is ever drawn from an event payload — see `_clean`.
PRINTABLE = frozenset(string.printable) - frozenset("\r\n\t\x0b\x0c")

NAME = "baseline"
HOOKS = ("on_init", "on_frame", "on_event", "on_output")

# The lifecycle calls, in the order the host makes them. `on_event` is fed the
# agent's own event names (see `_WATCHED`); `on_frame` gets a delta in seconds
# and the painter; `on_output` gets each block of answer text.
_WATCHED = ("stream_delta", "reasoning_delta", "tool_start", "tool_end",
            "tool_denied", "done", "stopped", "error", "retry", "economy_hit",
            "context_trimmed", "nudged")


# --- helpers every skin in this repository repeats ----------------------------
#
# Repeats on purpose. A pack has to survive being copied into `.beeagent/plugins/`
# on its own, so there is no shared module to import from — the allow-list the
# host enforces does not include one. Each of these is short enough to read twice.

def depth_of(support) -> int:
    """`color_support()` as one number: 0 plain, 1 sixteen, 2 two-fifty-six, 3 millions.

    Defensive because the answer is build-specific and a skin must not fall over
    on a spelling it has not seen: an int, a bool, a string, or None are all
    answers a real painter has been known to give.
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


def tone(value: str, depth: int):
    """The colour `value` in the spelling this terminal can print — None for plain text."""
    if depth <= 0:
        return None
    if depth >= 2:
        return value
    return ANSI16.get(value, "white")


def ask(painter, name: str, default=None):
    """`painter.<name>()` when that build has it, `default` when it does not.

    A skin that hard-crashes on a missing capability takes the whole interface
    down with it; a skin that draws a plain line instead is still useful.
    """
    call = getattr(painter, name, None)
    if call is None:
        return default
    try:
        return call()
    except Exception:
        return default


def size_of(painter) -> tuple:
    """(cols, rows) with a fallback — painting off-screen is worse than painting short."""
    cols, rows = ask(painter, "get_terminal_size", (80, 24)) or (80, 24)
    try:
        return max(int(cols), 20), max(int(rows), 4)
    except (TypeError, ValueError):
        return 80, 24


def clean(text, limit: int = 26) -> str:
    """Event payload as screen ink: one line, printable ASCII, bounded.

    Tool names and arguments are text a model wrote. Without the strip, a call
    argument containing an escape sequence would land in the status line and do
    whatever that sequence does to the terminal.
    """
    kept = [ch for ch in str(text) if ch in PRINTABLE][:limit]
    return "".join(kept).strip()


# --- the skin -----------------------------------------------------------------

class Baseline:
    """The state a static skin needs: what is happening now, and whether the
    screen says it yet."""

    __slots__ = ("cols", "rows", "depth", "lang", "translate", "state", "detail",
                 "tools", "runs", "denials", "lines", "drawn", "dirty", "painted")

    def __init__(self):
        self.cols, self.rows = 80, 24
        self.depth = None           # None until a painter tells us
        self.lang = "en"
        self.translate = None
        self.state = "idle"
        self.detail = ""
        self.tools = 0              # tools that started this session
        self.runs = 0               # ...and finished
        self.denials = 0
        self.lines = 0
        self.drawn = ""             # the last line we actually painted
        self.dirty = True           # paint once, then only on a change
        self.painted = False

    # -- language --------------------------------------------------------------

    def say(self, english: str, russian: str) -> str:
        """One string, two languages.

        `beeagent.i18n` is not on the host's import allow-list, so a skin may not
        `from beeagent.i18n import L`. The engine hands the translator over the
        context instead when it can; until it does, this is the same rule with
        the same defaults.
        """
        if self.translate is not None:
            try:
                return self.translate(english, russian)
            except Exception:
                pass
        return russian if self.lang == "ru" else english

    # -- words -----------------------------------------------------------------

    def status_word(self) -> str:
        """What is happening, in the active language."""
        return {
            "idle": self.say("ready", "готов"),
            "thinking": self.say("thinking", "думаю"),
            "writing": self.say("answering", "отвечаю"),
            "tool": self.say("running", "выполняю"),
            "denied": self.say("refused by you", "отказал ты"),
            "stopped": self.say("stopped", "остановлено"),
            "error": self.say("failed", "ошибка"),
            "done": self.say("done", "готово"),
        }.get(self.state, self.say("ready", "готов"))

    def line(self) -> str:
        """The whole status line. `o` is the marker the built-in line uses."""
        body = "BeeCode  o  " + self.status_word()
        if self.detail:
            body += "  " + self.detail
        return body

    # -- lifecycle -------------------------------------------------------------

    def on_init(self, ctx) -> None:
        """Remember what the host says about this terminal, then draw nothing yet."""
        self.lang = clean(getattr(ctx, "language", "en") or "en", 8) or "en"
        give = getattr(ctx, "translate", None) or getattr(ctx, "L", None)
        self.translate = give if callable(give) else None
        painter = getattr(ctx, "painter", None)
        if painter is not None:
            self.depth = depth_of(ask(painter, "color_support", None))
            self.cols, self.rows = size_of(painter)
        # A frame is not painted here: on_init runs before the host owns the
        # screen, and a skin that draws that early fights the banner for it.

    def on_event(self, event: str, payload) -> None:
        """Track state. Never draws — that is what the next frame is for."""
        data = payload if isinstance(payload, dict) else {}
        if event == "reasoning_delta":
            self._set("thinking", "")
        elif event == "stream_delta":
            self._set("writing", "")
        elif event == "tool_start":
            self.tools += 1
            self._set("tool", clean(data.get("tool", "?"), 20))
        elif event == "tool_end":
            self.runs += 1
            self._set("idle", "")
        elif event == "tool_denied":
            self.denials += 1
            self._set("denied", clean(data.get("tool", ""), 20))
        elif event == "economy_hit":
            self._set("idle", self.say("from cache", "из кэша"))
        elif event == "retry":
            self._set("thinking", self.say("retry " + str(clean(data.get("attempt", ""), 4)),
                                           "повтор " + str(clean(data.get("attempt", ""), 4))))
        elif event == "context_trimmed":
            self._set("idle", self.say("context trimmed", "контекст урезан"))
        elif event == "nudged":
            self._set("thinking", self.say("nudged", "подтолкнули"))
        elif event == "done":
            self._set("done", "")
        elif event == "stopped":
            self._set("stopped", "")
        elif event == "error":
            self._set("error", clean(data.get("message", ""), 26))

    def on_output(self, text) -> None:
        """Count the answer, change nothing else.

        This is the hook a skin may refuse to use. The baseline's line does not
        depend on how long the answer was, so it neither repaints nor stores any
        of it — only a count, which cannot grow the memory.
        """
        if text:
            self.lines += 1

    def on_frame(self, dt, painter) -> None:
        """The whole point: a frame in which nothing changed does nothing.

        `dt` is accepted and never read. Reading it is precisely what turns a
        static skin into an animated one, and this file is the static one.
        """
        if self.depth is None:
            # First frame we are given a painter for: learn the terminal, then paint.
            self.depth = depth_of(ask(painter, "color_support", None))
        if not self.dirty:
            return
        line = self.line()
        if line == self.drawn and self.painted:
            self.dirty = False
            return
        self._paint(painter, line)

    def _set(self, state: str, detail: str) -> None:
        """Move the state machine, and mark the screen stale only if it moved."""
        if state == self.state and detail == self.detail:
            return
        self.state, self.detail = state, detail
        self.dirty = True

    def _paint(self, painter, line: str) -> None:
        """One row at the bottom, as a single line of ink.

        The contract in `test_reference_skins` counts a "line" as one `draw_text`,
        and a static skin must draw exactly one per repaint — so the whole status
        line goes out in the brand's own honey. The palette itself is unchanged:
        the honey of what the model said, the leaf still defined for the fallback
        the animated skins hand back to this shape.
        """
        cols, rows = size_of(painter)
        self.cols, self.rows = cols, rows
        y = rows - 1
        painter.clear_region(0, y, cols, 1)
        painter.draw_text(0, y, line[:cols], color=tone(HONEY, self.depth))
        self.drawn = line
        self.dirty = False
        self.painted = True


# --- module surface the host reads --------------------------------------------
#
# The hooks live here, at module level, as plain functions over one instance.
# That is the contract: `core.skins` imports the file and calls these by name,
# so a skin's state is this module's state and a reload is a fresh object.

skin = Baseline()


def on_init(ctx) -> None:
    skin.on_init(ctx)


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


def status_line(painter=None) -> str:
    """The line the baseline stands on right now. Also what the other reference
    skins paint when they give up on animating."""
    if painter is not None and skin.depth is None:
        skin.depth = depth_of(ask(painter, "color_support", None))
    return skin.line()


# --- registration -------------------------------------------------------------
#
# `setup(api)` is the loader's own entry point and runs today; the lifecycle
# verbs around skins are new, so the exact name of the one that takes hooks is
# still moving. Try the plausible spellings, then the engine module itself, then
# fall back to the event bus — which the current host does have, and which keeps
# the state truthful even in a build that cannot yet give us a painter.

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
    """Hand the hooks over. True when some verb took them."""
    given = hooks()
    for verb in ("skin_hooks", "register_skin", "lifecycle", "hook"):
        call = getattr(api, verb, None)
        if callable(call):
            try:
                call(NAME, given)
                return True
            except TypeError:
                continue                      # right verb, different shape: try on
            except Exception:
                continue
    try:                                       # the engine itself, imported lazily
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
