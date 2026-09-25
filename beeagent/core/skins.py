"""Turn a skin from a colour dict into a programmable module.

A skin used to be a list of presets: pick a frame, pick a spinner, done. This is
the host that lets a skin be *code with access to the render cycle* — it can
animate a frame, react to a token arriving, to a tool call starting, to an
error — without the program having to trust it blindly.

    register("hive", skin)         # object | module | source str | legacy dict
    switch("hive")                 # -> on_init(ctx)
    post("stream_delta", {...})    # the UI's single event door -> on_event
    frame(painter, dt)             # the render loop's door -> on_frame
    emit_output("answer text")     # what the user is about to see -> on_output

The lifecycle contract, in the order a skin can observe it:

1. `register(name, skin)` stores the skin. No skin code runs here, with one
   deliberate exception: a skin handed over as *source text* (directly, or via
   `source=`) is checked by `check_source()` first and refused before it is ever
   compiled. Registering does not change what the user sees — choosing is a
   separate act from drawing, so nothing becomes active on its own.
2. `switch(name)` makes it active and calls `on_init(ctx)` once. A skin that
   raises in `on_init` is demoted on the spot and the baseline stays in force —
   a skin never half-replaces the interface.
3. `on_frame(dt, painter)` runs once per frame, from the render loop: `dt` is
   seconds since the previous frame, `painter` is `core/renderer.py`'s `Painter`
   (`draw_text`, `draw_box`, `clear_region`, `get_terminal_size`,
   `color_support`, `color`). That loop calls its callback as `paint(painter)`, so
   when it hands over only the painter, `frame()` measures `dt` itself. A frame
   with no active animation does nothing.
4. `post(event, payload)` is the single door agent events come through. The
   names are the ones `core/agent.py` really calls back with — see `EVENTS`.
5. `emit_output(text)` offers the text the UI is about to print. It is a copy: a
   skin that rewrites it changes nothing about what the user actually sees.
6. `unload(name)` forgets it; if it was active the baseline takes over silently,
   because you asked for it and a notice would be a lie about a surprise.

All four hooks are optional, and one may declare fewer parameters than the
contract offers: `on_frame(self)` is as callable as `on_frame(self, dt, painter)`.

Frames are painted by whoever owns the loop, not by us. `core/renderer.py`
supplies `Painter`, `Grid` and `FrameLoop`; we import it *lazily*, so this module
works whether that contract is importable in a given install or not — including
the older installs and the half-installed ones where it is missing. Without it,
`painter()` hands out a `NullPainter` that records what would have been drawn,
`new_grid()` returns a `DictGrid` with the same verbs, `start_loop()` reports that
there is no loop to start — and every hook, gate, budget and notice below behaves
exactly the same. Which side of that line we are on is readable from
`renderer_state()`, and `/skins` prints it.

What a skin is not allowed to do — and what that actually means
---------------------------------------------------------------
Read this before trusting it, because a mechanism nobody explained is a mechanism
somebody misuses.

**A gate on source text cannot contain a running program.** A skin is Python, in
this process, with this process's permissions. There is no sandbox here.
`exec`, `eval`, `compile` and `__import__` are refused *by name*, and a skin that
wanted them can spell them another way: `getattr` on a computed string,
`().__class__.__bases__[0]`, a lambda reaching its own `__globals__`, a string
handed to a function the gate had no reason to distrust. The gate parses syntax;
it does not evaluate it. It cannot see what a computed string becomes, it cannot
see what a C extension does, and it cannot be un-run — once a module object
exists, its top-level code already happened.

So this is not containment and is not sold as containment. It is a *refusal in
front of the user*, and it earns its place by three things it does well: it
catches every skin that stumbles into those names by accident, it forces a
hostile skin to go out of its way and look like it is doing so, and it prints the
offending line and token where the user can read them. The real check is the
human who read `plugin.py` and typed `/trust yes`: `core/trust.py`'s per-folder
gate and the `from_extension` grant already stand in front of every plugin that
can reach this module. The AST gate is the second, cheaper net.

Concretely, a skin module is refused before it runs when it imports anything
outside `IMPORT_ALLOWLIST`, uses a relative import, or *loads* a name from
`FORBIDDEN_NAMES` (the nine review-sensitive builtins) or any name starting with
an underscore — which is where the dunder escape routes live.

Budgets, stated as numbers
--------------------------
`on_frame` gets `FRAME_BUDGET_MS` (8 ms) and gets it every frame. An
over-budget frame is counted; `DEMOTE_AFTER_OVERRUNS` (3) in a row — or any single
frame over `FRAME_BUDGET_MS * HARD_CAP_FACTOR` (32 ms: that is not decoration,
that is a hang) — demotes the skin to the minimal baseline with one visible
notice naming the skin and the reason. The first frame after a switch has the
ceiling waived and `switch()` pays the renderer's cold-start costs up front,
because the slow thing in that frame is BeeCode's own imports, not the skin. A
hook that raises is demoted the same way, immediately, with the exception text
shown once. Never a silent switch, never a second notice.

Measuring a frame after it returned is not stopping it. Python offers no way to
preempt in-process code, so the policy is "one late frame, and then never again
from this skin", and it only protects what it can: the frame path is the one that
belongs to the render loop, so the thread waiting on a decoration is the UI's,
not the agent's. `post()` and `emit_output()` run inside the caller's callback by
design (a token stream cannot tolerate reordering), which is why they are timed
under the same clock, demote on the same hard cap, and catch every exception
before it can reach the loop that called us.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import shutil
import sys
import threading
import time
import types
from collections import deque
from dataclasses import dataclass, field

from beeagent.i18n import L

# ================================================================== events ===

#: The names `core/agent.py` hands to `callback(event, data)`, read off its call
#: sites. Not a wish list: a name absent from agent.py is absent here. `post()`
#: does not police the set — an unknown name is delivered and counted (see
#: `unknown_counts()`), because an emitter and a list can drift apart and dropping
#: an event silently is the failure nobody notices.
STREAM_DELTA = "stream_delta"
REASONING_DELTA = "reasoning_delta"
TOOL_START = "tool_start"
TOOL_END = "tool_end"
TOOL_DENIED = "tool_denied"
DONE = "done"
STOPPED = "stopped"
ERROR = "error"
RETRY = "retry"
ECONOMY_HIT = "economy_hit"
CONTEXT_TRIMMED = "context_trimmed"
NUDGED = "nudged"
STATUS = "status"
RESPONSE = "response"
WAITING = "waiting"
STREAM_RESET = "stream_reset"
MODEL_SWITCHED = "model_switched"
PROVIDER_FALLBACK = "provider_fallback"
QUEUED_SENT = "queued_sent"
TOOL_REPAIRED = "tool_repaired"
TOOL_DROPPED = "tool_dropped"
TOOL_RENAMED = "tool_renamed"
TOOL_UNKNOWN = "tool_unknown"
TOOL_ERROR = "tool_error"

#: The events a skin animates on.
CORE_EVENTS = frozenset({
    STREAM_DELTA, REASONING_DELTA, TOOL_START, TOOL_END, TOOL_DENIED, DONE,
    STOPPED, ERROR, RETRY, ECONOMY_HIT, CONTEXT_TRIMMED, NUDGED,
})
#: Everything agent.py emits, including the bookkeeping events (`tool_repaired`,
#: `model_switched`, ...). `EVENTS` is the whole vocabulary.
ALL_EVENTS = CORE_EVENTS | {
    STATUS, RESPONSE, WAITING, STREAM_RESET, MODEL_SWITCHED, PROVIDER_FALLBACK,
    QUEUED_SENT, TOOL_REPAIRED, TOOL_DROPPED, TOOL_RENAMED, TOOL_UNKNOWN,
    TOOL_ERROR,
}
EVENTS = ALL_EVENTS

# ============================================================= gate policy ====

#: A skin may import these and nothing else. The two BeeCode entries are the host
#: itself and the renderer contract it paints through.
IMPORT_ALLOWLIST = frozenset({
    "beeagent.core.skins",
    "beeagent.core.renderer",
    "math",
    "random",
    "time",
    "json",
    "dataclasses",
    "typing",
    "string",
    "itertools",
    "collections",
})

#: Of those, the ones that are packages, whose submodules a skin may also reach
#: (`collections.abc`, `typing.io`, and whatever the renderer grows).
ALLOW_SUBMODULES = frozenset({
    "beeagent.core.skins",
    "beeagent.core.renderer",
    "collections",
    "dataclasses",
    "typing",
})

#: Loaded by name in skin source -> refused. `__import__` and `compile` are the
#: two roads back into the gate's own machinery; `open`, `input` and `breakpoint`
#: reach the machine and the terminal; `exec`/`eval`/`globals`/`locals` reach this
#: module's namespace, which is where the budget and the allow-list live.
FORBIDDEN_NAMES = frozenset({
    "open", "exec", "eval", "__import__", "compile",
    "globals", "locals", "breakpoint", "input",
})

HOOKS = ("on_init", "on_frame", "on_event", "on_output")

# =========================================================== budget policy ====

#: Wall clock one `on_frame` may take, in milliseconds.
FRAME_BUDGET_MS = 8.0
#: Over-budget frames in a row that demote the skin to the baseline.
DEMOTE_AFTER_OVERRUNS = 3
#: One frame this many times over budget demotes at once, skipping the count.
HARD_CAP_FACTOR = 4.0
#: The same clock for the two hooks that run on someone else's thread.
EVENT_BUDGET_MS = 8.0

BASELINE = "baseline"

#: The slots a legacy colour dict may name; `ui/skin.py` owns their meaning.
UI_SLOTS = ("frame", "banner", "spinner", "stream")


# ================================================================= the gate ===

@dataclass
class Refusal:
    """One thing a skin module does that the gate refuses, with its location."""
    line: int
    token: str
    kind: str            # import | relative-import | name | dunder | syntax
    detail: str = ""

    def where(self) -> str:
        return L(f"line {self.line}", f"строка {self.line}")

    def why(self) -> str:
        if self.kind == "syntax":
            return L(f"it does not even parse: {_flat(self.detail)}",
                     f"он не разбирается: {_flat(self.detail)}")
        if self.kind == "relative-import":
            return L(f"a relative import ({self.token}) reaches outside the "
                     f"allow-list by construction",
                     f"относительный импорт ({self.token}) по построению уходит "
                     "за список разрешённого")
        if self.kind == "import":
            return L(f"it imports {self.token}, which is not on the allow-list",
                     f"он импортирует {self.token} — этого нет в списке "
                     "разрешённого")
        if self.kind == "dunder":
            return L(f"it reaches for {self.token}, and a dunder attribute is "
                     f"how code escapes an in-process gate",
                     f"он обращается к {self.token} — служебный атрибут с "
                     "двойным подчёркиванием это то, как код уходит из-под "
                     "проверки")
        return L(f"it uses the name {self.token}, which a skin may not use",
                 f"он использует имя {self.token}, недопустимое для скина")

    def __str__(self) -> str:
        return f"{self.where()}: {self.why()}"


def import_allowed(dotted: str) -> bool:
    """Is `dotted` an import a skin may write?"""
    if dotted in IMPORT_ALLOWLIST:
        return True
    for package in ALLOW_SUBMODULES:
        if dotted.startswith(package + "."):
            return True
    return False


def check_source(source) -> list:
    """Every refusal a skin module earns, before a byte of it runs.

    [] means the gate lets it through, which is a permission to *try* and not a
    promise of safety: read the module docstring.
    """
    if isinstance(source, (bytes, bytearray)):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as e:
            return [Refusal(0, "<bytes>", "syntax", detail=str(e))]
    if not isinstance(source, str):
        return [Refusal(0, type(source).__name__, "syntax", detail="not source text")]
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [Refusal(getattr(e, "lineno", 0) or 0, (getattr(e, "text", "") or "").strip(),
                        "syntax", detail=str(getattr(e, "msg", "") or e))]

    found: list = []
    for node in ast.walk(tree):
        # --- what it imports --------------------------------------------------
        if isinstance(node, ast.Import):
            for alias in node.names:
                if not import_allowed(alias.name):
                    found.append(Refusal(node.lineno, alias.name, "import"))
        elif isinstance(node, ast.ImportFrom):
            if node.level:                                   # from . / .. import
                found.append(Refusal(node.lineno,
                                     "." * node.level + (node.module or ""),
                                     "relative-import"))
                continue
            module = node.module or ""
            if not import_allowed(module):
                found.append(Refusal(node.lineno, module, "import"))
        # --- what it names ----------------------------------------------------
        elif isinstance(node, ast.Name):
            # Loads only: a skin may name a private variable, it may not read its
            # way out through one.
            if isinstance(node.ctx, ast.Load) and _forbidden_name(node.id):
                found.append(Refusal(node.lineno, node.id,
                                     "dunder" if node.id.startswith("__") else "name"))
        elif isinstance(node, ast.Attribute):
            # `__class__`, `__globals__`, `_getframe`: the escape routes that are
            # not builtin names. Refused on the attribute, not the value.
            if node.attr.startswith("_"):
                found.append(Refusal(node.lineno, node.attr, "dunder"))

    seen, out = set(), []
    for refusal in sorted(found, key=lambda r: (r.line, r.token)):
        key = (refusal.line, refusal.token, refusal.kind)
        if key not in seen:
            seen.add(key)
            out.append(refusal)
    return out


def _forbidden_name(name: str) -> bool:
    return name in FORBIDDEN_NAMES or name.startswith("_")


def refusal_text(name: str, refusals: list, limit: int = 3) -> str:
    """The sentence `/skins` and a failed switch print. Loud, on purpose."""
    head = L(f"skin “{name}” refused", f"скин «{name}» отклонён")
    body = "; ".join(str(r) for r in refusals[:limit])
    extra = len(refusals) - limit
    if extra > 0:
        body += L(f" (+{extra} more)", f" (и ещё {extra})")
    return f"{head}: {body}"


# ================================================ the lazy renderer bridge ====

class NullPainter:
    """The painter used while `core/renderer.py` cannot be imported.

    The method set the contract names, in the contract's shapes, so a skin
    written against it works unchanged once the real one lands: this one records
    instead of drawing.
    """

    def __init__(self):
        self.calls: list = []

    def draw_text(self, x, y, text, color=None, bg=None, style=None, **kwargs):
        self.calls.append(("draw_text", x, y, str(text), color, bg, style))
        return len(str(text))

    def draw_box(self, x, y, w, h, border=None, fill=None, title=None, **kwargs):
        self.calls.append(("draw_box", x, y, w, h, border, fill, title))
        return None

    def clear_region(self, x=0, y=0, w=None, h=None, **kwargs):
        self.calls.append(("clear_region", x, y, w, h))
        return None

    def get_terminal_size(self):
        cols, rows = shutil.get_terminal_size((80, 24))
        return (cols, rows)

    def color_support(self):
        return _color_support()

    def color(self, value, default=""):
        return default

    def warn(self, english, russian=None):
        self.calls.append(("warn", english))

    @property
    def frame_warnings(self):
        return []


class DictGrid:
    """`Grid` fallback: the cells in a dict, behind the real `Grid`'s verbs."""

    def __init__(self, cols: int = 80, rows: int = 24):
        self.cols, self.rows = int(cols), int(rows)
        self._cells: dict = {}

    def resize(self, cols, rows):
        self.cols, self.rows = int(cols), int(rows)
        return True

    def put(self, x, y, cell, *rest):
        self._cells[(int(x), int(y))] = cell
        return True

    def cell(self, x, y):
        return self._cells.get((int(x), int(y)))

    def blit(self, x, y, text, *rest, **kwargs):
        for offset, ch in enumerate(str(text)):
            self._cells[(int(x) + offset, int(y))] = ch
        return len(str(text))

    def fill(self, x, y, w, h, ch=" ", *rest, **kwargs):
        for column in range(int(x), int(x) + int(w)):
            for row in range(int(y), int(y) + int(h)):
                self._cells[(column, row)] = ch
        return None

    def line(self, y):
        return "".join(str(self._cells.get((x, int(y)), " "))[:1]
                       for x in range(self.cols))

    def clear(self):
        self._cells.clear()


RENDERER_IMPORT_NAME = "beeagent.core.renderer"

_PAINTER = None
_COLOR_SUPPORT = None


def _import_renderer():
    """Import the renderer now, or say why we could not. Never raises."""
    try:
        return importlib.import_module(RENDERER_IMPORT_NAME), ""
    except ImportError as e:                       # the contract is not there yet
        return None, str(e)
    except Exception as e:                         # present but broken
        return None, f"{type(e).__name__}: {e}"


def renderer_state() -> dict:
    """What paint calls actually reach — the real renderer, or our fallback."""
    module, error = _import_renderer()
    return {
        "available": module is not None,
        "error": error,
        "fallback": "" if module is not None else L(
            "NullPainter + DictGrid from beeagent.core.skins",
            "NullPainter + DictGrid из beeagent.core.skins"),
        "has": sorted(n for n in ("Painter", "Grid", "FrameLoop")
                      if module is not None and hasattr(module, n)),
    }


def painter():
    """The object `on_frame` is handed: a real `Painter` when there is one.

    Outside a frame loop the real Painter paints into a grid nobody emits, which
    is the same visible outcome as `NullPainter` — but it is the object the skin
    will get in production, so `cols`, `rows` and `grid` behave there. Built once
    and kept: making one costs more than the whole frame budget, and a skin must
    not be demoted for our own setup.
    """
    global _PAINTER
    module, _ = _import_renderer()
    if module is not None:
        if _PAINTER is not None and type(_PAINTER).__module__ == module.__name__:
            return _PAINTER
        try:
            factory = getattr(module, "default_painter", None)
            _PAINTER = factory() if callable(factory) else module.Painter()
            return _PAINTER
        except Exception:
            pass                        # a broken renderer is not the skin's fault
    if _PAINTER is None or type(_PAINTER) is not NullPainter:
        _PAINTER = NullPainter()
    return _PAINTER


def new_grid(cols=None, rows=None):
    """A `Grid`, or the dict-backed fallback with the same verbs."""
    width, height = _terminal_size()
    module, _ = _import_renderer()
    grid = getattr(module, "Grid", None) if module else None
    if isinstance(grid, type):
        try:
            return grid(cols or width, rows or height)
        except Exception:
            pass
    return DictGrid(cols or width, rows or height)


def _terminal_size() -> tuple:
    try:
        size = painter().get_terminal_size()
        if size and int(size[0]) > 0 and int(size[1]) > 0:
            return (int(size[0]), int(size[1]))
    except Exception:
        pass
    return shutil.get_terminal_size((80, 24))


def _color_support() -> str:
    global _COLOR_SUPPORT
    if _COLOR_SUPPORT is None:
        try:
            from rich.console import Console
            _COLOR_SUPPORT = str(getattr(Console(), "color_system", None) or "none")
        except Exception:
            _COLOR_SUPPORT = "none"
    return _COLOR_SUPPORT


# ================================================================= registry ===

@dataclass
class Entry:
    """One registered skin and everything the host remembers about it."""
    name: str
    skin: object = None
    description: str = ""
    refusals: list = field(default_factory=list)
    module_name: str = ""
    demoted: bool = False
    demote_reason: str = ""
    notice: str = ""
    notice_shown: bool = False
    initialized: bool = False
    frames: int = 0
    frame_overruns: int = 0
    frame_consecutive: int = 0
    slowest_frame_ms: float = 0.0
    events: int = 0
    event_overruns: int = 0
    unknown: dict = field(default_factory=dict)
    errors: int = 0
    outputs: int = 0

    @property
    def refused(self) -> bool:
        return bool(self.refusals)

    def hooks(self) -> tuple:
        return hooks_of(self.skin)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "active": active_name() == self.name,
            "kind": kind_of(self.skin),
            "description": self.description,
            "hooks": list(self.hooks()),
            "refused": refusal_text(self.name, self.refusals) if self.refused else "",
            "demoted": self.demoted,
            "reason": self.demote_reason,
            "frames": self.frames,
            "frame_overruns": self.frame_overruns,
            "slowest_frame_ms": round(self.slowest_frame_ms, 2),
            "events": self.events,
            "event_overruns": self.event_overruns,
            "unknown_events": dict(self.unknown),
            "errors": self.errors,
            "outputs": self.outputs,
        }


_REGISTRY: dict = {}
_ACTIVE = ""
#: Everything the host announced, newest last; `take_notice()` drains it.
_NOTICES = deque(maxlen=50)
#: Who prints them. The UI takes over with `set_notifier`.
_NOTIFIER = None
_LOOP: dict = {}
_COUNTS = {"post": 0, "delivered": 0, "unknown": 0, "overruns": 0,
           "demotions": 0, "notices": 0, "refused": 0, "switches": 0}
#: Every event name `post()` was sent that is not in `EVENTS`, whoever was active.
_UNKNOWN: dict = {}
_LAST_FRAME = time.monotonic()

_BUDGET = {"frame_ms": FRAME_BUDGET_MS, "overruns": DEMOTE_AFTER_OVERRUNS,
           "hard_factor": HARD_CAP_FACTOR, "event_ms": EVENT_BUDGET_MS}


def configure(frame_budget_ms=None, demote_after_overruns=None,
              hard_cap_factor=None, event_budget_ms=None) -> dict:
    """Move the budget, and report what it is. A skin cannot call this: the
    allow-list gives it no route to this module's namespace."""
    if frame_budget_ms is not None:
        _BUDGET["frame_ms"] = max(0.0, float(frame_budget_ms))
    if demote_after_overruns is not None:
        _BUDGET["overruns"] = max(1, int(demote_after_overruns))
    if hard_cap_factor is not None:
        _BUDGET["hard_factor"] = max(1.0, float(hard_cap_factor))
    if event_budget_ms is not None:
        _BUDGET["event_ms"] = max(0.0, float(event_budget_ms))
    return dict(_BUDGET)


def budget() -> dict:
    return dict(_BUDGET)


def hooks_of(skin) -> tuple:
    """Which lifecycle hooks this skin actually implements, in contract order."""
    if skin is None:
        return ()
    return tuple(h for h in HOOKS if callable(_hook(skin, h)))


def kind_of(skin) -> str:
    """legacy (a colour/slot dict) | module | object | inert | refused."""
    if skin is None:
        return "refused"
    if isinstance(skin, dict):
        return "legacy"
    if isinstance(skin, types.ModuleType):
        return "module"
    return "object" if hooks_of(skin) else "inert"


def _hook(skin, name):
    try:
        found = getattr(skin, name, None)
    except Exception:                               # a property that throws
        return None
    return found if callable(found) else None


def _accepts(func, offered: int) -> int:
    """How many of the contract's arguments this hook is willing to take."""
    try:
        params = list(inspect.signature(func).parameters.values())
    except (TypeError, ValueError):                  # builtins, C functions
        return offered
    if any(p.kind == inspect.Parameter.VAR_POSITIONAL for p in params):
        return offered
    needed = [p for p in params
              if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD)
              and p.default is inspect.Parameter.empty]
    return min(offered, max(0, len(needed)))


def _active_entry():
    """The Entry actually in force, or None when the baseline is."""
    entry = _REGISTRY.get(_ACTIVE)
    if entry is None or entry.refused or entry.demoted:
        return None
    return entry


# ------------------------------------------------------------------ listing --

def names() -> list:
    return list(_REGISTRY)


def entries() -> list:
    """The baseline first, then every registered skin."""
    return [_baseline_entry()] + list(_REGISTRY.values())


def get(name: str):
    """The skin object registered under `name`, or None."""
    entry = _REGISTRY.get(name)
    return None if entry is None or entry.refused else entry.skin


def active():
    """The skin in force: the chosen one, or the baseline from `ui/skin.py`.

    Never None and it never raises — an empty registry is the normal state of a
    BeeCode with no skin plugin installed.
    """
    entry = _active_entry()
    return baseline() if entry is None else entry.skin


def active_name() -> str:
    entry = _active_entry()
    return BASELINE if entry is None else entry.name


def is_active(name: str) -> bool:
    return active_name() == name


def baseline() -> dict:
    """The shipped interface as the fallback source: the live slot choices.

    `ui/skin.py` is where BeeCode's own looks actually live — the frame, banner,
    spinner and stream variants, plus whatever a plugin registered into them. A
    colour dict is read on top of it, so a legacy skin that names no slot for the
    banner keeps whatever the user chose there.
    """
    values = {}
    try:
        from beeagent.ui import skin as ui_skin
        values.update(ui_skin.as_dict() or {})
    except Exception:
        # No UI module is not a reason to break a hook call: the names ui/skin.py
        # ships with, hardcoded as the last resort.
        values.update({"frame": "rounded", "banner": "shimmer",
                       "spinner": "honey", "stream": "default"})
    return values


def _baseline_entry() -> Entry:
    """The row `/skins` shows for the interface BeeCode ships with."""
    return Entry(name=BASELINE, skin=baseline(), description=L(
        "BeeCode's own slots, no animation",
        "Штатные слоты BeeCode, без анимации"))


def colors(name: str = "", key: str = "", default=""):
    """A colour from a legacy dict skin; the active palette when no name is given.

    A dict skin carries its colours under `colors`/`colours`/`palette`/`styles`,
    or as plain string values at the top level next to its slot names.
    """
    skin = active() if not name else get(name)
    if skin is None:
        skin = baseline()
    if not isinstance(skin, dict):
        return default if key else {}
    tables = _palettes(skin)
    if key:
        for table in tables:
            if key in table:
                return table[key]
        return default
    merged = {}
    for table in tables:
        merged.update(table)
    return merged


def _palettes(skin: dict) -> list:
    tables = [skin[k] for k in ("colors", "colours", "palette", "styles")
              if isinstance(skin.get(k), dict)]
    tables.append({k: v for k, v in skin.items()
                   if isinstance(k, str) and isinstance(v, str) and k not in UI_SLOTS})
    return tables


# ----------------------------------------------------------------- registering

def register(name: str, skin=None, description: str = "", source=None,
             check: bool = True) -> Entry:
    """Add a skin, and store why it was refused if it was.

    `skin` may be an object or module with hooks, a legacy colour/slot dict, or
    source *text*. With `source=` (or a string as `skin`) the AST gate runs before
    a byte compiles, and a refusal is recorded in place of a skin so `/skins` can
    show the line and the token.

    Registering nothing becomes active: `switch()` is a separate act, the same
    separation `ui/skin.py` draws between choosing and drawing.

    Handing over a live module or object is an act of the embedding program, and
    means the bytes were already vouched for by whoever imported them — the same
    reading `plugins/loader.py` gives a `PluginLoader` built by hand. Anything off
    disk should come in through `install_source()`.
    """
    clean = (name or "").strip()
    entry = Entry(name=clean or "?", description=description or "")
    if not clean:
        entry.refusals = [Refusal(0, "<empty>", "syntax", detail=L(
            "a skin has to be registered under a name",
            "скин надо зарегистрировать под именем"))]
        return entry
    if isinstance(skin, (str, bytes, bytearray)) and source is None:
        source, skin = skin, None
    if check and source is not None:
        refusals = check_source(source)
        if refusals:
            entry.refusals = refusals
            _REGISTRY[clean] = entry
            _COUNTS["refused"] += 1
            return entry
        module_name = f"beeagent_skin_{safe_module_name(clean)}"
        loaded = _compile_source(clean, source, module_name)
        if isinstance(loaded, Refusal):
            entry.refusals = [loaded]
            _REGISTRY[clean] = entry
            _COUNTS["refused"] += 1
            return entry
        skin = loaded
        entry.module_name = module_name
    previous = _REGISTRY.get(clean)
    entry.skin = skin
    _REGISTRY[clean] = entry
    if previous is not None and _ACTIVE == clean:
        # Re-registering the skin on screen — the reload path, and the only way a
        # stopped skin promised to come back. Treat it as a fresh switch, so its
        # `on_init` runs against the new object instead of it sitting active and
        # uninitialised.
        switch(clean)
    return entry


def install_source(name: str, source, description: str = "") -> Entry:
    """Register a skin from source text, gated first. The plugin path."""
    return register(name, source=source, description=description)


def _compile_source(name: str, source, module_name: str = ""):
    """Run gated source into a module. Any failure is a Refusal, not an exception."""
    module_name = module_name or f"beeagent_skin_{safe_module_name(name)}"
    module = types.ModuleType(module_name)
    module.__doc__ = f"skin '{name}' registered by beeagent.core.skins"
    try:
        code = compile(source, f"<skin:{name}>", "exec")
    except SyntaxError as e:
        return Refusal(getattr(e, "lineno", 0) or 0, "", "syntax", detail=str(e))
    sys.modules[module_name] = module
    try:
        exec(code, module.__dict__)                  # noqa: S102 — past the gate
    except BaseException as e:
        sys.modules.pop(module_name, None)
        return Refusal(getattr(e, "lineno", 0) or 0, type(e).__name__, "syntax",
                       detail=f"{type(e).__name__}: {e}")
    return module


def safe_module_name(name: str) -> str:
    """A module-name-safe skin name, without an `re` import at module level."""
    return "".join(c if c.isalnum() or c == "_" else "_" for c in str(name)).strip("_") or "skin"


def unload(name: str) -> bool:
    """Forget a skin. If it was active, the baseline takes over silently."""
    global _ACTIVE, _LAST_FRAME
    entry = _REGISTRY.pop(name, None)
    if entry is None:
        return False
    if entry.module_name:
        sys.modules.pop(entry.module_name, None)
    if _ACTIVE == entry.name:
        _ACTIVE = ""
        _LAST_FRAME = time.monotonic()
    return True


def reset() -> None:
    """Empty the registry: the state of an install with no skin plugin at all."""
    for entry in list(_REGISTRY.values()):
        if entry.module_name:
            sys.modules.pop(entry.module_name, None)
    _REGISTRY.clear()
    _NOTICES.clear()
    _UNKNOWN.clear()
    global _ACTIVE, _LAST_FRAME
    _ACTIVE = ""
    _LAST_FRAME = time.monotonic()
    for key in _COUNTS:
        _COUNTS[key] = 0


# ------------------------------------------------------------------ switching --

def switch(name: str) -> str:
    """Put a skin in force. "" on success, the reason it did not otherwise.

    The reason is what `/skins <name>` prints, and what a refused skin's sentence
    looks like: never a silent no.
    """
    global _ACTIVE, _LAST_FRAME
    word = (name or "").strip()
    if word.lower() in ("", "none", "off", "reset", "default", BASELINE):
        _ACTIVE = ""
        _LAST_FRAME = time.monotonic()
        return ""
    entry = _REGISTRY.get(word) or _REGISTRY.get(word.lower())
    if entry is None:
        return L(f"no skin “{word}” — /skins lists what there is",
                 f"скина «{word}» нет — список по /skins")
    if entry.refused:
        _COUNTS["refused"] += 1
        return refusal_text(entry.name, entry.refusals)
    if entry.demoted:
        return L(f"skin “{entry.name}” is stopped: {entry.demote_reason} — it runs "
                 f"again only if it is registered fresh",
                 f"скин «{entry.name}» остановлен: {entry.demote_reason} — он "
                 "запустится, только если зарегистрировать его заново")

    _ACTIVE = entry.name
    _COUNTS["switches"] += 1
    _LAST_FRAME = time.monotonic()
    _apply_legacy(entry)
    _warm()
    init = _hook(entry.skin, "on_init")
    if init is None:
        return ""
    started = time.perf_counter()
    try:
        _call(init, (Context(entry.name),))
        entry.initialized = True
    except Exception as e:
        return _demote(entry, L(f"on_init raised {type(e).__name__}: {_flat(e)}",
                                f"on_init поднял {type(e).__name__}: {_flat(e)}"))
    # A one-time setup cost is counted, never fatal: `on_init` runs once per
    # switch, so a slow first call has no chance to hurt the session the way a
    # slow `on_event` — once per token — does.
    _guard_event(entry, (time.perf_counter() - started) * 1000.0, "on_init",
                 demote=False)
    return ""


def _warm() -> None:
    """Pay the import and detection costs before a timer is running.

    Importing `core/renderer.py`, building the first `Painter` and asking the
    terminal what it can do together cost far more than an 8 ms frame budget on a
    cold start. Doing it at switch time — off the hot path, once — is what keeps
    the budget honest: otherwise the first skin of the session is the one charged
    for BeeCode's own imports.
    """
    try:
        _import_renderer()
        _color_support()
        _terminal_size()
    except Exception:
        pass


def _apply_legacy(entry: Entry) -> None:
    """A colour/slot dict skin reaches the interface the way it always did."""
    if not isinstance(entry.skin, dict):
        return
    try:
        from beeagent.ui import skin as ui_skin
    except Exception:
        return
    for slot in UI_SLOTS:
        value = entry.skin.get(slot)
        if isinstance(value, str):
            try:
                ui_skin.choose(slot, value, source=f"skin:{entry.name}")
            except Exception:
                pass                       # an unknown variant is not an outage


def _demote(entry: Entry, why: str) -> str:
    """Stop a skin, say so once, and hand the screen back to the baseline."""
    global _ACTIVE
    entry.demoted = True
    entry.demote_reason = why
    entry.errors += 1
    if not entry.notice_shown:
        entry.notice_shown = True
        _COUNTS["demotions"] += 1
        entry.notice = L(
            f"skin “{entry.name}” was stopped and the plain interface is back: "
            f"{why}. /skins lists the rest; this one runs again only if you "
            f"register it afresh.",
            f"скин «{entry.name}» остановлен, включено обычное оформление: {why}. "
            "Остальные — в /skins; этот заработает только если зарегистрировать "
            "его заново.")
        _announce(entry.notice)
    if _ACTIVE == entry.name:
        _ACTIVE = ""
    return why


def _guard_event(entry, elapsed_ms: float, where: str, demote: bool = True) -> bool:
    """Book a non-frame hook call against the clock. Returns True when over."""
    if entry is None:
        return False
    if elapsed_ms <= _BUDGET["event_ms"]:
        return False
    entry.event_overruns += 1
    _COUNTS["overruns"] += 1
    if demote and elapsed_ms >= _BUDGET["frame_ms"] * _BUDGET["hard_factor"]:
        # The caller is the agent's own thread: one event at the ceiling there is a
        # hang, and it repeats every token, so it is not counted three times.
        _demote(entry, L(f"{where} took {elapsed_ms:.1f} ms on the caller's thread",
                         f"{where} занял {elapsed_ms:.1f} мс в нити вызывающего"))
    return True


# ------------------------------------------------------------------- notices ---

def set_notifier(func) -> None:
    """Give the UI control of how a notice reaches the user. None restores it."""
    global _NOTIFIER
    _NOTIFIER = func


def _announce(text: str) -> None:
    _NOTICES.append(text)
    _COUNTS["notices"] += 1
    if _NOTIFIER is not None:
        try:
            _NOTIFIER(text)
        except Exception:
            pass                       # a notice is not worth breaking a render
        return
    try:
        from rich.console import Console
        Console().print(f"⚠ {text}")
    except Exception:
        pass                           # cp1251, no tty, no rich: stay silent


def take_notice() -> str:
    """The notice the host owes the user, or "". Draining it is the point."""
    return _NOTICES.popleft() if _NOTICES else ""


def notices() -> list:
    """Every notice still owed. `listing()` and the tests read this."""
    return list(_NOTICES)


def notice_for(name: str) -> str:
    entry = _REGISTRY.get(name)
    return entry.notice if entry is not None else ""


def clear_notices() -> None:
    _NOTICES.clear()


# -------------------------------------------------------------------- events ---

@dataclass
class Context:
    """What `on_init` is handed: who the skin is, and the doors it may use later."""
    skin: str

    def name(self) -> str:
        return self.skin

    def size(self) -> tuple:
        return _terminal_size()

    def colors(self, key: str = "", default=""):
        return colors(self.skin, key, default)

    def post(self, event: str, payload=None) -> bool:
        return post(event, payload)

    def stats(self) -> dict:
        return stats(self.skin)


_Context = Context          # the older name; a skin may hold either


def _call(func, args: tuple):
    """Hand a hook as many of the contract's arguments as it declares."""
    return func(*list(args)[:_accepts(func, len(args))])


def post(event: str, payload=None) -> bool:
    """The single door agent events come through. True if the skin saw this one.

    An unknown event name is delivered and counted exactly like a known one: the
    emitter in `core/agent.py` is the source of truth, and a list that falls
    behind it must not become a reason for a skin to miss something. The count is
    readable from `unknown_counts()` and printed by `/skins`.
    """
    _COUNTS["post"] += 1
    name = str(event or "")
    known = name in ALL_EVENTS
    if not known:
        _COUNTS["unknown"] += 1
        _UNKNOWN[name] = _UNKNOWN.get(name, 0) + 1
    entry = _active_entry()
    if entry is not None:
        entry.events += 1
        if not known:
            entry.unknown[name] = entry.unknown.get(name, 0) + 1
    hook = _hook(active(), "on_event")
    if hook is None:
        return False
    data = payload if isinstance(payload, dict) else (
        {} if payload is None else {"data": payload})
    started = time.perf_counter()
    try:
        _call(hook, (name, data))
    except Exception as e:
        if entry is not None:
            _demote(entry, L(f"on_event(“{name}”) raised {type(e).__name__}: "
                             f"{_flat(e)}",
                             f"on_event(«{name}») поднял {type(e).__name__}: "
                             f"{_flat(e)}"))
        return False
    _guard_event(entry, (time.perf_counter() - started) * 1000.0,
                 f"on_event(“{name}”)")
    _COUNTS["delivered"] += 1
    return True


def emit_output(text: str) -> bool:
    """Offer the outgoing text to the skin. True if it saw it.

    The skin receives a copy and its return value is dropped: `on_output` may
    animate from what is about to appear, it may not edit what does.
    """
    entry = _active_entry()
    if entry is not None:
        entry.outputs += 1
    hook = _hook(active(), "on_output")
    if hook is None:
        return False
    started = time.perf_counter()
    try:
        _call(hook, (str(text),))
    except Exception as e:
        if entry is not None:
            _demote(entry, L(f"on_output raised {type(e).__name__}: {_flat(e)}",
                             f"on_output поднял {type(e).__name__}: {_flat(e)}"))
        return False
    _guard_event(entry, (time.perf_counter() - started) * 1000.0, "on_output")
    return True


def _charge(entry, elapsed_ms: float) -> bool:
    """Book one frame against its budget. True when it went over."""
    if elapsed_ms <= _BUDGET["frame_ms"]:
        if entry is not None:
            entry.frame_consecutive = 0
        return False
    _COUNTS["overruns"] += 1
    if entry is None:
        return True
    entry.frame_overruns += 1
    entry.frame_consecutive += 1
    return True


# -------------------------------------------------------------------- frames ---

def frame(*args, **kwargs) -> float:
    """The render loop's door: one frame of the active skin.

    Tolerates either argument order, because `core/renderer.py` owns the callback
    shape: `frame(painter, dt)` and `frame(dt, painter)` read the same way, and so
    does `frame(dt=..., painter=...)`. `dt` defaults to the seconds since the last
    frame. Returns this frame's wall time in milliseconds.
    """
    global _LAST_FRAME
    now = time.monotonic()
    dt = kwargs.get("dt", kwargs.get("seconds"))
    chosen = kwargs.get("painter")
    for value in args:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if dt is None:
                dt = float(value)
        elif chosen is None and hasattr(value, "draw_text"):
            chosen = value
    if dt is None:
        dt = max(0.0, now - _LAST_FRAME)
    _LAST_FRAME = now

    entry = _active_entry()
    hook = _hook(active(), "on_frame")
    if hook is None:
        return 0.0                         # no animation: nothing to draw, nothing to time
    if entry is not None:
        entry.frames += 1
    offered = (float(dt), painter() if chosen is None else chosen)
    started = time.perf_counter()
    try:
        _call(hook, offered)
    except Exception as e:
        elapsed = (time.perf_counter() - started) * 1000.0
        if entry is not None:
            _charge(entry, elapsed)
            _demote(entry, L(f"on_frame raised {type(e).__name__}: {_flat(e)}",
                             f"on_frame поднял {type(e).__name__}: {_flat(e)}"))
        return elapsed
    elapsed = (time.perf_counter() - started) * 1000.0
    if entry is None:
        return elapsed
    entry.slowest_frame_ms = max(entry.slowest_frame_ms, elapsed)
    over = _charge(entry, elapsed)
    hard_cap = _BUDGET["frame_ms"] * _BUDGET["hard_factor"]
    # The first frame after a switch gets the ceiling waived: an import the skin
    # never asked for can land there, and the count-up rule below still catches a
    # skin that is genuinely slow.
    if over and entry.frames > 1 and elapsed >= hard_cap:
        _demote(entry, L(
            f"a frame took {elapsed:.1f} ms, past the {hard_cap:.0f} ms "
            f"ceiling — that is a hang, not decoration",
            f"кадр занял {elapsed:.1f} мс, больше потолка {hard_cap:.0f} мс — "
            "это зависание, а не украшение"))
    elif over and entry.frame_consecutive >= _BUDGET["overruns"]:
        _demote(entry, L(
            f"{entry.frame_consecutive} frames in a row went over the "
            f"{_BUDGET['frame_ms']:g} ms budget (last: {elapsed:.1f} ms)",
            f"{entry.frame_consecutive} кадров подряд вышли за бюджет "
            f"{_BUDGET['frame_ms']:g} мс (последний: {elapsed:.1f} мс)"))
    return elapsed


#: The name a render loop is likeliest to reach for. Same function.
tick = frame


# ----------------------------------------------------------- our own loop -----

def start_loop(fps: int = 12):
    """Take a `FrameLoop` from `core/renderer.py` and paint the active skin.

    Returns (running, message). The loop is the only thing that calls `frame()`
    here, which is what keeps a slow skin off the thread that runs the agent:
    `FrameLoop.run` blocks, so it goes on a daemon thread of its own, and
    `stop_loop()` ends it with the loop's own `stop()`. The loop's `frame_warnings`
    — why a glyph or a colour was dropped — are handed to the user when it stops,
    rather than dying with the object.

    Without the renderer there is nothing to start and this says so; the host
    still works, driven by whoever calls `frame()`.
    """
    module, error = _import_renderer()
    loop_class = getattr(module, "FrameLoop", None) if module else None
    if loop_class is None:
        return (False, L(
            f"no render loop: {RENDERER_IMPORT_NAME} supplies no FrameLoop "
            f"({error or 'not installed'}), so skin frames are painted only while "
            f"the UI calls frame()",
            f"цикла отрисовки нет: {RENDERER_IMPORT_NAME} не даёт FrameLoop "
            f"({error or 'не установлен'}) — кадры рисует только вызов frame() из "
            "интерфейса"))
    if _LOOP.get("loop") is not None:
        stop_loop()
    sink: dict = {}
    made = None
    for build in (lambda: loop_class(_paint_for_loop),
                  lambda: loop_class(paint=_paint_for_loop),
                  lambda: loop_class(on_frame=_paint_for_loop)):
        try:
            made = build()
            break
        except Exception:
            continue
    if made is None:
        try:
            made = loop_class()
        except Exception as e:
            return (False, L(f"FrameLoop took neither (paint) nor (): {e}",
                             f"FrameLoop не принял ни (paint), ни (): {e}"))
        for setter in ("set_paint", "set_callback", "attach"):
            func = getattr(made, setter, None)
            if callable(func):
                try:
                    func(_paint_for_loop)
                    break
                except Exception:
                    continue
    runner = getattr(made, "run", None)
    if not callable(runner):
        return (False, L("FrameLoop has no run(): the frame loop stays off and "
                         "the UI drives frame() itself",
                         "у FrameLoop нет run(): цикл остаётся выключен, интерфейс "
                         "вызывает frame() сам"))
    thread = threading.Thread(target=_drive_loop, args=(made, int(fps), sink),
                             name="beeagent-skin-frames", daemon=True)
    _LOOP.update({"loop": made, "stop": getattr(made, "stop", None),
                  "fps": int(fps), "thread": thread, "sink": sink})
    thread.start()
    return (True, L(f"frame loop running at {fps} fps, painting the active skin",
                    f"цикл идёт с {fps} к/с, рисует активный скин"))


def _paint_for_loop(painter=None):
    """The callback shape `FrameLoop` uses: one painter, `dt` measured here."""
    return frame(painter)


def _drive_loop(loop, fps: int, sink: dict) -> None:
    """Run the loop on its own thread. It never raises; we add one more guard."""
    try:
        result = loop.run(fps=fps)
        if isinstance(result, dict):
            sink.update(result)
    except BaseException as e:                     # a dead loop must not kill the app
        sink["error"] = f"{type(e).__name__}: {e}"


def stop_loop(timeout: float = 2.0) -> bool:
    """Stop our frame loop, say what it saw, and forget it."""
    stop = _LOOP.get("stop")
    loop = _LOOP.get("loop")
    thread = _LOOP.get("thread")
    sink = dict(_LOOP.get("sink") or {})
    _LOOP.clear()
    if loop is None:
        return False
    if callable(stop):
        try:
            stop()
        except Exception:
            return False
    if thread is not None and thread.is_alive():
        thread.join(timeout)
    # The renderer counts the reasons a frame looked different; hand them over.
    for warning in list(getattr(loop, "frame_warnings", ()) or ())[-3:]:
        _announce(L(f"render loop: {_flat(warning)}",
                    f"цикл отрисовки: {_flat(warning)}"))
    if sink.get("error"):
        _announce(L(f"render loop stopped: {sink['error']}",
                    f"цикл отрисовки упал: {sink['error']}"))
    _LOOP["last"] = sink
    return True


def loop_running() -> bool:
    thread = _LOOP.get("thread")
    return bool(_LOOP.get("loop")) and bool(thread and thread.is_alive())


def loop_stats() -> dict:
    """What our own frame loop reports, if there is one. Includes its warnings."""
    loop = _LOOP.get("loop")
    if loop is None:
        return {"running": False, "renderer": renderer_state()["available"],
                "last": dict(_LOOP.get("last") or {})}
    report = dict(_LOOP.get("sink") or {})
    report.update({
        "running": loop_running(),
        "fps": getattr(loop, "fps", _LOOP.get("fps")),
        "frame_warnings": list(getattr(loop, "frame_warnings", ()) or ())[-5:],
        "frames": getattr(loop, "frames", report.get("frames")),
        "overrun_frames": getattr(loop, "overrun_frames", report.get("overrun")),
        "paint_errors": getattr(loop, "paint_errors", report.get("paint_errors")),
    })
    return report


# ------------------------------------------------------------------ reports ----

def unknown_counts() -> dict:
    """Event names `post()` was sent that are not in `EVENTS`, and how often.

    Counted whatever skin was active — a name nobody knew is exactly the thing
    that must not go missing because the active skin had no hook to receive it.
    """
    return dict(_UNKNOWN)


def is_known_event(name: str) -> bool:
    return str(name or "") in ALL_EVENTS


def stats(name: str = "") -> dict:
    """The host's counters, or one skin's record."""
    if name:
        entry = _REGISTRY.get(name)
        return entry.as_dict() if entry else {}
    out = dict(_COUNTS)
    out.update({
        "active": active_name(),
        "registered": len(_REGISTRY),
        "refused": sum(1 for e in _REGISTRY.values() if e.refused),
        "demoted": sum(1 for e in _REGISTRY.values() if e.demoted),
        "budget_ms": _BUDGET["frame_ms"],
        "demote_after": _BUDGET["overruns"],
        "hard_cap_ms": _BUDGET["frame_ms"] * _BUDGET["hard_factor"],
        "unknown_events": unknown_counts(),
        "renderer": renderer_state(),
        "loop": loop_stats(),
    })
    return out


def _flat(text) -> str:
    return " ".join(str(text).split())[:200]


# ========================================================= /skins command =====

COMMAND_NAME = "skins"
USAGE = "/skins [name]"
DESCRIPTION = "Skins that are code: list them, switch, see why one was refused"


def register_command() -> bool:
    """Put `/skins` into the shared command machinery.

    The `core/trust.py` trick: both interfaces go through `commands.dispatch`, so
    registering into `COMMANDS`/`HANDLERS` from here covers the classic REPL, the
    Textual sidebar and one-shot runs, and commands.py owns nothing but the one
    call. The category is a core one so a plugin cannot take the name back out,
    and it is idempotent because it runs at import time.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_skins:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="engine")
    core.HANDLERS[COMMAND_NAME] = _cmd_skins
    return True


def _cmd_skins(ctx, args):
    """`/skins` lists, `/skins <name>` switches, `/skins off` takes the baseline back."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    if args:
        reason = switch(args[0])
        if reason:
            return CommandResult(output=Text(reason, style="bold red"))
        return CommandResult(output=Text(
            L(f"skin: {active_name()}", f"скин: {active_name()}"), style="bold"))
    return CommandResult(output=Text(listing()))


def listing() -> str:
    """Every skin the host knows: the active one marked, the refused ones said why.

    A plain aligned block with ASCII markers, so it reads the same in the TUI
    sidebar, in the classic REPL and in a test:
    `*` in force, `!` refused before it ran, `x` stopped by the budget or a raise.
    """
    rows = []
    active = active_name()
    order = [BASELINE] + [name for name in names() if name != BASELINE]
    for name in order:
        entry = _baseline_entry() if name == BASELINE else _REGISTRY.get(name)
        if entry is None:
            continue
        mark = ("*" if active == entry.name else
                "!" if entry.refused else "x" if entry.demoted else " ")
        if entry.refused:
            note = refusal_text(entry.name, entry.refusals)
        elif entry.demoted:
            note = L(f"stopped: {entry.demote_reason}",
                     f"остановлен: {entry.demote_reason}")
        else:
            hooks = ", ".join(entry.hooks()) or L("no hooks", "без хуков")
            if entry.name == BASELINE:
                slots = " ".join(f"{slot}={value}" for slot, value in
                                 sorted(entry.skin.items()))
                note = L(f"the shipped slots ({slots})",
                         f"штатные слоты ({slots})")
            elif kind_of(entry.skin) == "legacy":
                chosen = " ".join(f"{slot}={entry.skin[slot]}" for slot in UI_SLOTS
                                  if isinstance(entry.skin.get(slot), str))
                note = L(f"a colour/slot dict ({chosen})" if chosen
                         else "a colour/slot dict",
                         f"цвета/слоты ({chosen})" if chosen else "цвета/слоты")
            else:
                note = hooks
            if entry.description:
                note = f"{entry.description} · {note}"
        rows.append((mark, entry.name, note, _tally(entry)))

    if not rows:
        return L("no skins registered", "скинов не зарегистрировано")
    width = max(len(r[1]) for r in rows)
    lines = [L("skins  (* in force, ! refused, x stopped)",
               "скины  (* действует, ! отклонён, x остановлен)")]
    for mark, name, note, tally in rows:
        lines.append(f" {mark} {name.ljust(width)}  {note}{tally}")
    numbers = budget()
    footer = L(f"budget {numbers['frame_ms']:g} ms per frame, "
               f"{numbers['hard_factor']:g}x is a hang, demote after "
               f"{numbers['overruns']} in a row",
               f"бюджет {numbers['frame_ms']:g} мс на кадр, "
               f"{numbers['hard_factor']:g}x — это зависание, отключение после "
               f"{numbers['overruns']} подряд")
    renderer = renderer_state()
    footer += L(f" · renderer: {'core.renderer' if renderer['available'] else renderer['fallback']}",
                f" · отрисовка: {'core.renderer' if renderer['available'] else renderer['fallback']}")
    unknown = unknown_counts()
    if unknown:
        footer += L(f" · unknown events counted: {', '.join(sorted(unknown))}",
                    f" · незнакомых событий: {', '.join(sorted(unknown))}")
    lines.extend(["", footer])
    return "\n".join(lines)


def _tally(entry) -> str:
    if entry.name == BASELINE:
        return ""
    bits = []
    if entry.frames:
        bits.append(L(f"{entry.frames} frames", f"{entry.frames} кадров"))
    if entry.frame_overruns:
        bits.append(L(f"{entry.frame_overruns} over budget",
                      f"{entry.frame_overruns} сверх бюджета"))
    if entry.events:
        bits.append(L(f"{entry.events} events", f"{entry.events} событий"))
    if entry.unknown:
        bits.append(L(f"{sum(entry.unknown.values())} unknown",
                      f"{sum(entry.unknown.values())} незнакомых"))
    return f"  [{' · '.join(bits)}]" if bits else ""
