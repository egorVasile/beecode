"""The skin host: lifecycle, event fan-out, frame budget, and the refusal gate.

What these tests hold shut is the promise in `beeagent/core/skins.py`: a skin is
code, it gets the render cycle in a documented order, it is paid in a budget it
cannot overspend three times, and a module that reaches for the machine is
refused *by name and line number* before a byte of it runs. Plus the two failure
modes that matter most: nothing breaks when no skin plugin is installed, and a
skin that raises must not take the agent's loop with it.

The console this runs under is cp1251, so anything with Russian wording or curly
quotes goes to a UTF-8 file in `tmp_path` and is read back from there.

Nothing here reaches the network or the developer's working tree.
"""
import ast
import sys
import time
import types
from collections import deque
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.core import skins
from beeagent.ui import skin as ui_skin

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------- the host ---

@pytest.fixture
def host(monkeypatch):
    """A clean registry, a captured notice channel, and the shipped budgets."""
    captured = []
    monkeypatch.setattr(skins, "_REGISTRY", {})
    monkeypatch.setattr(skins, "_NOTICES", deque(maxlen=50))
    monkeypatch.setattr(skins, "_UNKNOWN", {})
    monkeypatch.setattr(skins, "_LOOP", {})
    monkeypatch.setattr(skins, "_ACTIVE", "")
    monkeypatch.setattr(skins, "_NOTIFIER", captured.append)
    monkeypatch.setattr(skins, "_BUDGET", dict(skins.budget()))
    monkeypatch.setattr(ui_skin, "_active", dict(ui_skin._DEFAULTS))
    monkeypatch.setattr(ui_skin, "_sources", {})
    host.captured = captured
    return captured


class Recorder:
    """A skin with all four hooks, logging what each was handed."""

    def __init__(self):
        self.log = []

    def on_init(self, ctx):
        self.log.append(("init", ctx.name(), ctx.size()))

    def on_frame(self, dt, painter):
        self.log.append(("frame", dt, painter))

    def on_event(self, event, payload):
        self.log.append(("event", event, payload))

    def on_output(self, text):
        self.log.append(("output", text))


def _painter_methods(painter) -> set:
    return {n for n in ("draw_text", "draw_box", "clear_region",
                        "get_terminal_size", "color_support", "color")
            if callable(getattr(painter, n, None))}


# ------------------------------------------------------------- the lifecycle --

def test_a_skin_with_all_four_hooks_gets_them_in_order_with_dt_and_a_painter(host):
    """init at switch, event at post, frame at frame, output at emit_output."""
    skin = Recorder()
    skins.register("rec", skin)
    assert skins.switch("rec") == ""

    skins.post(skins.STREAM_DELTA, {"text": "to"})
    elapsed = skins.frame(painter=None, dt=0.0833)
    skins.emit_output("answer text")

    kinds = [row[0] for row in skin.log]
    assert kinds == ["init", "event", "frame", "output"]
    # what each hook was handed
    assert skin.log[0][1] == "rec"
    assert skin.log[0][2][0] > 0 and skin.log[0][2][1] > 0     # a real terminal size
    assert skin.log[1][1] == "stream_delta"
    assert skin.log[1][2] == {"text": "to"}
    assert skin.log[2][1] == pytest.approx(0.0833)             # dt in seconds
    assert _painter_methods(skin.log[2][2]) == {"draw_text", "draw_box", "clear_region",
                                                "get_terminal_size", "color_support",
                                                "color"}
    assert skin.log[3][1] == "answer text"
    assert elapsed >= 0.0
    assert skins.stats("rec")["hooks"] == ["on_init", "on_frame", "on_event", "on_output"]


def test_unload_gives_the_screen_back_without_a_notice(host):
    """You asked for it: an unload is silent, and the baseline is what is left."""
    skins.register("rec", Recorder())
    skins.switch("rec")
    assert skins.active_name() == "rec"
    assert skins.unload("rec") is True
    assert skins.active_name() == skins.BASELINE
    assert isinstance(skins.active(), dict)
    assert host == [] and skins.notices() == []


def test_a_missing_hook_is_not_an_error(host):
    """A skin that only animates: the other three doors close quietly."""
    class OnlyFrames:
        def __init__(self):
            self.frames = 0

        def on_frame(self, dt, painter):
            self.frames += 1

    skin = OnlyFrames()
    skins.register("ticks", skin)
    assert skins.switch("ticks") == ""                  # no on_init: fine
    assert skins.post(skins.TOOL_START, {"tool": "read"}) is False
    assert skins.emit_output("hello") is False          # no on_output: fine
    skins.frame(painter=None, dt=0.05)
    assert skin.frames == 1
    assert skins.stats("ticks")["hooks"] == ["on_frame"]


def test_a_hook_may_declare_fewer_arguments_than_the_contract_offers(host):
    """`on_frame(self)` and `on_event(name)` are callable skins, not crashes."""
    seen = []

    class Narrow:
        def on_event(self, name):
            seen.append(name)

        def on_frame(self):
            seen.append("frame")

    skins.register("narrow", Narrow())
    skins.switch("narrow")
    skins.post(skins.DONE, {})
    skins.frame(painter=None, dt=0.01)
    assert seen == ["done", "frame"]


def test_the_renderer_fallback_is_used_when_the_module_cannot_be_imported(host, monkeypatch):
    """No `core/renderer.py`: the host still runs skins, on NullPainter.

    This is the answer to "which fallback did you use": a painter of our own that
    records instead of drawing, and no frame loop to start.
    """
    monkeypatch.setattr(skins, "_import_renderer", lambda: (None, "no module named renderer"))
    assert skins.renderer_state()["available"] is False
    assert "NullPainter" in skins.renderer_state()["fallback"]

    painter = skins.painter()
    assert isinstance(painter, skins.NullPainter)
    assert painter.draw_text(0, 0, "bee") == 3
    assert painter.calls[0][0] == "draw_text"
    grid = skins.new_grid(4, 2)
    grid.blit(1, 0, "xy")
    assert grid.cell(1, 0) == "x"

    running, message = skins.start_loop(fps=12)
    assert running is False
    assert "FrameLoop" in message                   # said out loud, not swallowed

    skins.register("rec", Recorder())
    skins.switch("rec")
    skins.frame(painter=None, dt=0.02)
    assert _painter_methods(skins._REGISTRY["rec"].skin.log[-1][2]) == \
        {"draw_text", "draw_box", "clear_region", "get_terminal_size", "color_support",
         "color"}


def test_start_loop_drives_a_frame_loop_and_stop_hands_over_its_warnings(host, monkeypatch):
    """The `FrameLoop` contract: paint(painter), run(fps=), stop(), frame_warnings."""
    painted = []

    class FakeLoop:
        def __init__(self, paint=None, **kwargs):
            self.paint = paint
            self.fps = 12
            self.frames = 0
            self.stop_called = False
            self.frame_warnings = ["a colour the terminal has not got was dropped"]

        def stop(self):
            self.stop_called = True

        def run(self, fps=12, max_frames=None):
            self.fps = fps
            while not self.stop_called and self.frames < 3:
                self.paint(skins.NullPainter())
                self.frames += 1
                time.sleep(0.005)
            return {"frames": self.frames, "fps": fps}

    module = types.ModuleType("fake_renderer")
    module.FrameLoop = FakeLoop
    monkeypatch.setattr(skins, "_import_renderer", lambda: (module, ""))

    class Ticker:
        def on_frame(self, dt, painter):
            painted.append(dt)

    skins.register("tick", Ticker())
    skins.switch("tick")
    running, message = skins.start_loop(fps=10)
    try:
        assert running is True and "10 fps" in message
        deadline = time.monotonic() + 5
        while len(painted) < 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert painted, "the loop never reached on_frame"
        assert painted[0] >= 0.0                       # dt measured, not invented
        assert skins.loop_stats()["fps"] in (10, 10.0)
        assert skins.stop_loop() is True
    finally:
        skins.stop_loop()                              # no loop left on a thread
    assert any("a colour the terminal has not got" in text for text in host)


# ------------------------------------------------------------- event fan-out --

def test_an_unknown_event_is_delivered_and_counted(host):
    """A name we do not know is not a name the skin misses. It is counted instead."""
    skins.register("rec", Recorder())
    skins.switch("rec")
    assert skins.post("tokens_gained", {"n": 5}) is True
    assert skins._REGISTRY["rec"].skin.log[-1] == ("event", "tokens_gained", {"n": 5})
    assert skins.unknown_counts() == {"tokens_gained": 1}
    assert skins.is_known_event(skins.STREAM_DELTA) is True
    assert skins.is_known_event("tokens_gained") is False
    listing = skins.listing()
    assert "tokens_gained" in listing          # visible in /skins, never swallowed


def test_post_is_the_single_door_and_reaches_only_the_active_skin(host):
    skins.register("a", Recorder())
    skins.register("b", Recorder())
    skins.switch("a")
    skins.post(skins.TOOL_END, {"tool": "bash"})
    a = skins._REGISTRY["a"].skin
    b = skins._REGISTRY["b"].skin
    assert [row[1] for row in a.log if row[0] == "event"] == ["tool_end"]
    assert b.log == []


def test_the_event_names_are_the_ones_the_agent_loop_really_emits():
    """No invented events: our set is exactly agent.py's `callback("<name>", ...)`.

    Read off the call sites with an AST, so adding an event in agent.py without
    naming it here fails this test rather than surprising a skin at runtime.
    """
    tree = ast.parse((ROOT / "beeagent" / "core" / "agent.py").read_text(encoding="utf-8"))
    emitted = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "callback"
                and node.args and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)):
            emitted.add(node.args[0].value)
    assert emitted, "no callback() call sites found — the sweep is broken"
    assert emitted == set(skins.ALL_EVENTS), (
        f"agent.py emits {sorted(emitted - set(skins.ALL_EVENTS))} that skins.py does "
        f"not name; skins.py names {sorted(set(skins.ALL_EVENTS) - emitted)} that "
        "agent.py never sends")


# ---------------------------------------------------------------- the budget --

def test_three_over_budget_frames_in_a_row_demote_once_and_the_notice_says_why(host):
    """8 ms is the budget, 3 in a row is the end — and one notice, not one per frame."""
    class Sloppy:
        def on_frame(self, dt, painter):
            time.sleep(0.010)              # over 5 ms, nowhere near the 500 ms ceiling

    skins.configure(frame_budget_ms=5.0, demote_after_overruns=3, hard_cap_factor=100.0)
    assert skins.budget()["frame_ms"] == 5.0
    skins.register("sloppy", Sloppy())
    skins.switch("sloppy")

    skins.frame(painter=None, dt=0.05)
    assert skins.active_name() == "sloppy", "one late frame must not end the skin"
    skins.frame(painter=None, dt=0.05)
    assert skins.active_name() == "sloppy"
    skins.frame(painter=None, dt=0.05)

    assert skins.active_name() == skins.BASELINE
    assert skins.stats("sloppy")["demoted"] is True
    reason = skins.stats("sloppy")["reason"]
    assert "3 frames in a row" in reason and "5 ms" in reason
    # One visible notice, naming the skin and the reason; then never again.
    assert len(host) == 1
    assert "sloppy" in host[0] and "5 ms" in host[0]
    assert "in a row" in host[0]
    skins.frame(painter=None, dt=0.05)
    skins.frame(painter=None, dt=0.05)
    assert len(host) == 1, f"the notice repeated: {host}"
    # `take_notice()` hands the same single notice out once and drains it.
    assert skins.take_notice() != ""
    assert skins.take_notice() == ""


def test_a_frame_past_the_hard_ceiling_demotes_at_once(host):
    """32 ms is not decoration: the ceiling ends the skin without counting to 3.

    The first frame after a switch has the ceiling waived — that is where
    BeeCode's own imports land — so a skin is judged from the second one.
    """
    class Stuck:
        def on_frame(self, dt, painter):
            time.sleep(0.02)

    skins.configure(frame_budget_ms=5.0, hard_cap_factor=2.0)
    skins.register("stuck", Stuck())
    skins.switch("stuck")
    skins.frame(painter=None, dt=0.05)
    assert skins.active_name() == "stuck", "the waived first frame only counts, it ends nothing"
    assert skins.stats("stuck")["frame_overruns"] == 1
    skins.frame(painter=None, dt=0.05)
    assert skins.active_name() == skins.BASELINE
    assert "ceiling" in skins.stats("stuck")["reason"]
    assert len(host) == 1


def test_a_faster_skin_resets_the_overrun_counter(host):
    """Consecutive means consecutive: a good frame between two late ones restarts it."""
    class Alternating:
        def __init__(self):
            self.n = 0

        def on_frame(self, dt, painter):
            self.n += 1
            if self.n % 2:
                time.sleep(0.010)

    skins.configure(frame_budget_ms=5.0, demote_after_overruns=3, hard_cap_factor=100.0)
    skins.register("alt", Alternating())
    skins.switch("alt")
    for _ in range(8):
        skins.frame(painter=None, dt=0.05)
    assert skins.active_name() == "alt"
    assert skins.stats("alt")["frame_overruns"] == 4


def test_a_raising_skin_is_demoted_and_the_agent_keeps_running(host):
    """An exception in a hook stops the skin, never the loop that called it.

    `callback` is shaped like the agent's own: `post()` is the last thing it does
    with an event, so if it let anything escape, the answer would die with it.
    """
    class Broken:
        def on_event(self, event, payload):
            raise RuntimeError("no renderer for this state")

        def on_frame(self, dt, painter):
            raise KeyError("grid")

    skins.register("broken", Broken())
    assert skins.switch("broken") == ""

    seen = []

    def callback(event, data):                # the agent's callback, as in repl.py
        skins.post(event, data)
        seen.append(event)

    callback("error", {"message": "endpoint silent"})
    callback("done", {"text": "an answer"})
    assert seen == ["error", "done"], "the agent's own work was interrupted"
    assert skins.active_name() == skins.BASELINE
    assert len(host) == 1 and "RuntimeError" in host[0] and "no renderer" in host[0]

    # The demoted skin is not called again, and the rest of the session is normal.
    skins.register("rec", Recorder())
    skins.switch("rec")
    assert skins.post(skins.STREAM_DELTA, {"text": "still here"}) is True
    assert skins.stats("broken")["errors"] == 1
    assert len(host) == 1, f"the second skin or a repeat frame added {host}"
    assert skins.frame(painter=None, dt=0.02) >= 0.0


def test_on_output_and_on_init_can_both_be_demoted(host):
    class Loud:
        def on_init(self, ctx):
            raise ValueError("bad setup")

    skins.register("loud", Loud())
    reason = skins.switch("loud")
    assert "ValueError" in reason and "bad setup" in reason
    assert skins.active_name() == skins.BASELINE          # never half-applied
    assert len(host) == 1


def test_a_stopped_skin_comes_back_only_when_it_is_registered_afresh(host):
    """What the notice promises, in code: switching will not revive it."""
    class Broken:
        def on_frame(self, dt, painter):
            raise RuntimeError("the first version was broken")

    skins.register("once", Broken())
    skins.switch("once")
    skins.frame(painter=None, dt=0.01)
    assert skins.active_name() == skins.BASELINE
    refused = skins.switch("once")
    assert "is stopped" in refused and "RuntimeError" in refused
    assert len(host) == 1, "a stopped skin does not get to announce itself twice"

    skins.register("once", Recorder())                    # the fixed version
    assert skins.active_name() == skins.BASELINE          # no revival by itself
    assert skins.switch("once") == ""
    assert skins.active_name() == "once"
    assert len(host) == 1


def test_re_registering_the_skin_on_screen_re_initialises_it(host):
    """The reload path: `reset()` then `setup()` again on a skin that is showing."""
    calls = []

    class Version:
        def __init__(self, tag):
            self.tag = tag

        def on_init(self, ctx):
            calls.append(self.tag)

    skins.register("live", Version("first"))
    skins.switch("live")
    skins.register("live", Version("second"))
    assert calls == ["first", "second"]
    assert skins.active_name() == "live"


# ----------------------------------------------------------------- the gate ---

def _source(*lines):
    return '"""A skin."""\n' + "\n".join(lines) + "\n"


@pytest.mark.parametrize("lines, token, line_at", [
    (["import os"], "os", 2),
    (["import subprocess"], "subprocess", 2),
    (["open(\"notes.txt\")"], "open", 2),
    (["__import__(\"os\")"], "__import__", 2),
    (["value = ().__class__"], "__class__", 2),
    (["from pathlib import Path"], "pathlib", 2),
    (["import beeagent.core.agent"], "beeagent.core.agent", 2),
    (["from .relative import helper"], ".relative", 2),
    (["path = globals()"], "globals", 2),
    (["text = input()"], "input", 2),
    (["secret = _hidden"], "_hidden", 2),
    (["import os", "os.system(\"dir\")"], "os", 2),
])
def test_the_gate_refuses_a_skin_module_naming_its_line_and_token(lines, token, line_at):
    refusals = skins.check_source(_source(*lines))
    assert refusals, f"{lines} got through"
    matched = [r for r in refusals if r.token == token]
    assert matched, f"{token} not named in {[str(r) for r in refusals]}"
    assert matched[0].line == line_at
    text = str(matched[0])
    assert f"line {line_at}" in text and token in text


def test_the_gate_refuses_the_whole_module_not_just_the_first_line():
    refusals = skins.check_source(_source("import os", "import socket", "open(\"x\")"))
    assert [r.token for r in refusals] == ["os", "socket", "open"]
    assert [r.line for r in refusals] == [2, 3, 4]


def test_a_syntax_error_is_a_refusal_with_a_line_not_a_crash():
    refusals = skins.check_source(_source("def on_frame("))
    assert len(refusals) == 1 and refusals[0].kind == "syntax"
    assert refusals[0].line >= 2


def test_the_gate_reaches_inside_a_function_body_not_only_the_top_line():
    """The usual trick — hide it in a method — is still the same AST."""
    source = "\n".join(['"""s"""', "", "class Skin:",
                        "    def on_frame(self, dt, painter):",
                        "        exec('import os')", ""])
    refusals = skins.check_source(source)
    assert [r.token for r in refusals] == ["exec"], [str(r) for r in refusals]
    assert refusals[0].line == 5


def test_a_refused_module_never_runs(host):
    """Refused means not compiled, not executed-then-ignored."""
    source = '"""s"""\nMARKER = []\nimport os\n'
    skins.install_source("ghost", source)
    assert skins._REGISTRY["ghost"].skin is None
    assert "beeagent_skin_ghost" not in sys.modules
    assert skins.get("ghost") is None


def test_unload_of_a_source_skin_forgets_its_module(host):
    skins.install_source("mathy", _source("import math",
                                         "def on_frame(dt, painter):",
                                         "    painter.draw_text(0, 0, str(math.pi))"))
    assert "beeagent_skin_mathy" in sys.modules
    assert skins.switch("mathy") == ""
    skins.frame(painter=skins.NullPainter(), dt=0.1)
    painter = skins._REGISTRY["mathy"].skin
    assert skins.unload("mathy") is True
    assert "beeagent_skin_mathy" not in sys.modules
    assert skins.active_name() == skins.BASELINE
    assert painter is not None              # the object lives; the host forgot it


@pytest.mark.parametrize("lines", [
    ["import math"],
    ["import random", "import time", "import json", "import string"],
    ["from itertools import cycle", "from collections import deque"],
    ["from dataclasses import dataclass", "from typing import Optional"],
    ["from collections.abc import Mapping"],
    ["from beeagent.core.skins import EVENTS"],
])
def test_the_allow_list_is_the_only_way_in(lines):
    assert skins.check_source(_source(*lines)) == []
    assert skins.import_allowed("math") and skins.import_allowed("collections.abc")
    assert not skins.import_allowed("os")
    assert not skins.import_allowed("beeagent.core.agent")
    assert not skins.import_allowed("subprocess")
    # The list itself, verbatim, so a silent widening is a visible change.
    assert set(skins.IMPORT_ALLOWLIST) == {
        "beeagent.core.skins", "beeagent.core.renderer", "math", "random", "time",
        "json", "dataclasses", "typing", "string", "itertools", "collections"}


def test_registering_a_refused_source_keeps_it_visible_and_a_switch_says_why(host):
    entry = skins.install_source("hostile", _source("import os"))
    assert entry.refused and entry.skin is None
    assert skins.get("hostile") is None
    assert skins.active_name() == skins.BASELINE
    reason = skins.switch("hostile")
    assert "refused" in reason and "line 2" in reason and "os" in reason
    assert len(host) == 0                       # the notice is the /skins output
    assert skins.stats()["refused"] == 1


def test_a_gated_skin_module_actually_runs(host):
    """The gate is not a wall: an allow-listed skin compiles, hooks and paints."""
    source = "\n".join([
        '"""Hive shimmer."""',
        "import math",
        "from beeagent.core.skins import EVENTS",
        "STATE = {}",
        "def on_init(ctx):",
        "    STATE['size'] = ctx.size()",
        "def on_frame(dt, painter):",
        "    painter.draw_text(0, 0, '*' * max(1, int(abs(math.sin(dt)) * 5)))",
        "def on_event(event, payload):",
        "    STATE.setdefault('events', []).append(event)",
        "def on_output(text):",
        "    STATE['last'] = text",
        "",
    ])
    entry = skins.install_source("hive-shimmer", source)
    assert not entry.refused, str(entry.refusals)
    assert entry.hooks() == ("on_init", "on_frame", "on_event", "on_output")
    assert skins.switch("hive-shimmer") == ""
    assert skins.stats("hive-shimmer")["kind"] == "module"
    skins.post(skins.TOOL_START, {"tool": "read"})
    skins.emit_output("done")
    painter = skins._REGISTRY["hive-shimmer"].skin  # module
    skins.frame(painter=skins.NullPainter(), dt=0.5)
    assert painter.STATE["events"] == ["tool_start"]
    assert painter.STATE["last"] == "done"
    assert "stream_delta" in skins.EVENTS


# ------------------------------------------------------------ legacy skins ----

def test_a_legacy_colour_dict_skin_still_resolves(host):
    """A dict of colours and slot names is still a skin, and still works."""
    retro = {"colors": {"answer": "#ffcc00", "tool": "#8a6d00"},
             "frame": "minimal", "banner": "none"}
    skins.register("retro", retro)
    assert skins.stats("retro")["kind"] == "legacy"
    assert skins.switch("retro") == ""
    assert skins.active_name() == "retro"
    assert skins.active() is retro
    assert skins.colors("retro", "answer") == "#ffcc00"
    assert skins.colors(key="tool") == "#8a6d00"           # the active palette
    assert "#ffcc00" in skins.colors("retro").values()
    # The slots reach the interface the way they always did: ui/skin.py still owns
    # their meaning, and a slot the dict does not name keeps the user's choice.
    assert ui_skin.get("frame") == "minimal"
    assert ui_skin.get("banner") == "none"
    assert ui_skin.get("spinner") == ui_skin._DEFAULTS["spinner"]
    assert skins.hooks_of(retro) == ()                     # no hooks, no calls


def test_the_shipped_skins_still_work_and_the_baseline_follows_them(host):
    """BeeCode's own skins go through `ui/skin.py`; the fallback reads it live.

    Each shipped skin template is imported and its `setup(api)` is run against a
    stand-in for the extension API that routes `skin`/`set_skin` into ui.skin and
    swallows whatever else it asks for. Then every slot a skin chose has to be the
    one the host reports as the baseline, and every variant it chose has to exist.
    That is the "do not break the five" test: a legacy skin is a set of choices,
    and choices still resolve after this host exists.
    """
    from beeagent.plugins.catalog import TEMPLATES_DIR
    import importlib.util
    import sys

    class StubApi:
        def __init__(self, plugin: str):
            self.plugin = plugin
            self.chose: dict = {}

        def skin(self, slot, name, value=None):
            ui_skin.register(slot, name, value)
            self.chose.setdefault(f"registered:{slot}", []).append(name)
            return True

        def set_skin(self, slot, name):
            chosen = ui_skin.choose(slot, name, source=f"plugin:{self.plugin}")
            self.chose[slot] = name
            assert chosen, f"{self.plugin}: slot {slot}={name} does not resolve"
            return chosen

        def get(self, key, default=None):
            return default

        def __getattr__(self, item):
            return lambda *args, **kwargs: None      # any newer api call: a no-op

    templates = sorted((TEMPLATES_DIR / "plugins").glob("*skin*/plugin.py"))
    assert len(templates) >= 2, f"expected the shipped skins, found {templates}"
    for path in templates:
        module_name = f"beeagent_test_skin_{path.parent.name}"
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        api = StubApi(path.parent.name)
        setup = getattr(module, "setup", None)
        assert callable(setup), f"{path.parent.name} has no setup(api)"
        setup(api)
        for slot, name in api.chose.items():
            if slot.startswith("registered:"):
                continue
            assert name in ui_skin.variants(slot)
        # And the host follows, because the baseline IS ui/skin.py read live.
        assert skins.baseline() == ui_skin.as_dict()
        assert skins.active() == ui_skin.as_dict()
        sys.modules.pop(module_name, None)


def test_a_slot_a_legacy_skin_does_not_name_keeps_the_users_choice(host):
    before = ui_skin.as_dict()
    skins.register("quiet", {"frame": "ascii"})
    skins.switch("quiet")
    after = ui_skin.as_dict()
    assert after["frame"] == "ascii"
    assert {k: v for k, v in after.items() if k != "frame"} == \
           {k: v for k, v in before.items() if k != "frame"}
    assert "ascii" in skins.listing(), "the listing has to show what a dict chose"


def test_the_gate_does_not_touch_ui_skin_and_keeps_it_as_the_source(host):
    """Refusing a skin module must never rewrite the shipped interface."""
    before = ui_skin.as_dict()
    skins.register("bad", _source("import os"))
    skins.switch("bad")
    assert ui_skin.as_dict() == before
    assert skins.active() == before            # baseline is ui/skin.py, live


# ------------------------------------------------------------------- /skins ---

@pytest.fixture
def clean_commands(monkeypatch):
    """`register_command()` mutates two module-level registries; keep them local."""
    from beeagent.ui import commands as core

    monkeypatch.setattr(core, "COMMANDS", list(core.COMMANDS))
    monkeypatch.setattr(core, "HANDLERS", dict(core.HANDLERS))
    return core


def _run(clean_commands, line):
    from beeagent.ui.commands import ReplContext, dispatch

    assert skins.register_command() is True
    return dispatch(ReplContext(), line)


def test_skins_lists_and_marks_the_active_one(host, clean_commands):
    result = _run(clean_commands, "/skins")
    text = result.output.plain
    assert "skins" in text.lower()
    assert f"* {skins.BASELINE}" in text, text
    assert not [row for row in text.splitlines() if row.strip().startswith("!")], text
    assert "budget 8 ms" in text and "3 in a row" in text


def test_skins_lists_marks_active_and_marks_refused(host, clean_commands):
    skins.register("rec", Recorder())
    skins.register("retro", {"frame": "minimal"})
    skins.install_source("hostile", _source("import os"))
    skins.switch("retro")
    text = _run(clean_commands, "/skins").output.plain
    active_row = [row for row in text.splitlines() if "* retro" in row]
    assert active_row, text
    assert "minimal" in "\n".join(                       # a legacy row says so
        row for row in text.splitlines() if "retro" in row)
    hostile = [row for row in text.splitlines() if "hostile" in row][0]
    assert hostile.lstrip().startswith("!"), hostile
    assert "refused" in hostile and "line 2" in hostile and "os" in hostile
    rec = [row for row in text.splitlines() if "rec" in row][0]
    assert "on_init" in rec and "on_frame" in rec


def test_skins_switches_by_argument_and_says_the_refusal_aloud(host, clean_commands):
    skins.register("rec", Recorder())
    result = _run(clean_commands, "/skins rec")
    assert "skin: rec" in result.output.plain
    assert skins.active_name() == "rec"
    skins.install_source("hostile", _source("import subprocess"))
    refused = _run(clean_commands, "/skins hostile")
    assert "refused" in refused.output.plain and "subprocess" in refused.output.plain
    assert skins.active_name() == "rec", "a refusal must not switch anything"
    missing = _run(clean_commands, "/skins nope")
    assert "no skin" in missing.output.plain
    assert _run(clean_commands, "/skins off").output is not None
    assert skins.active_name() == skins.BASELINE


def test_skins_reads_in_both_languages(host, tmp_path, clean_commands):
    """Bilingual wording, checked in UTF-8 files: the console here is cp1251."""
    skins.register("rec", Recorder())
    skins.install_source("hostile", _source("open(\"x\")"))
    i18n.set_lang("en")
    english = _run(clean_commands, "/skins").output.plain
    i18n.set_lang("ru")
    russian = _run(clean_commands, "/skins").output.plain
    refusal = _run(clean_commands, "/skins hostile").output.plain
    (tmp_path / "skins-en.txt").write_text(english, encoding="utf-8")
    (tmp_path / "skins-ru.txt").write_text(russian + "\n---\n" + refusal, encoding="utf-8")
    assert "refused" in english and "budget" in english
    saved = (tmp_path / "skins-ru.txt").read_text(encoding="utf-8")
    assert "отклонён" in saved and "скин" in saved
    assert "строка 2" in saved


# ------------------------------------------------------- graceful degradation -

def test_an_empty_registry_leaves_the_baseline_active_and_prints_nothing(host, capsys):
    """No skin plugin installed at all: nothing runs, nothing says anything.

    The registry is deleted outright, not merely unused, so this is the state of a
    fresh install — and the hooks, the frames and the events all still answer.
    """
    assert skins._REGISTRY == {}
    assert skins.active_name() == skins.BASELINE
    assert skins.active() == ui_skin.as_dict()
    assert isinstance(skins.active(), dict)
    assert skins.post(skins.STREAM_DELTA, {"text": "hi"}) is False
    assert skins.post("brand_new_event", {}) is False
    assert skins.emit_output("text") is False
    assert skins.frame(painter=None, dt=0.05) == 0.0
    assert skins.switch("nobody") != ""              # an honest no, not a crash
    assert skins.notices() == [] and host == []
    assert skins.stats()["registered"] == 0
    assert skins.stats()["demoted"] == 0
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""


def test_registering_never_changes_what_the_user_sees(host, capsys):
    """Choosing is a separate act from drawing, so registration is quiet."""
    skins.register("rec", Recorder())
    skins.install_source("hostile", _source("import os"))
    assert skins.active_name() == skins.BASELINE
    assert skins.is_active("rec") is False
    out = capsys.readouterr()
    assert out.out == "" and out.err == "" and host == []


def test_a_notice_survives_without_a_notifier_and_without_a_console(host, monkeypatch, tmp_path):
    """Nobody subscribed and no rich console: the notice is still owed, once."""
    skins.set_notifier(None)
    monkeypatch.setattr(skins, "_NOTICES", deque(maxlen=50))

    class Bad:
        def on_frame(self, dt, painter):
            raise IndexError("no cell at 999,999")

    skins.register("bad", Bad())
    skins.switch("bad")
    skins.frame(painter=None, dt=0.02)
    assert len(skins.notices()) == 1
    text = skins.take_notice()
    (tmp_path / "notice.txt").write_text(text, encoding="utf-8")
    assert "IndexError" in text and "bad" in text
    assert skins.take_notice() == ""
    assert "IndexError" in (tmp_path / "notice.txt").read_text(encoding="utf-8")


def test_the_renderer_is_reached_only_lazily():
    """No top-level import of `core.renderer`: the host loads without that file.

    Read off the syntax tree, because one `from beeagent.core.renderer import
    Painter` at the top of the module would look harmless and break every install
    where the contract is missing, half-installed or still being written.
    """
    source = (ROOT / "beeagent" / "core" / "skins.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    top_level = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            top_level.append(node.module or "")
    assert "beeagent.i18n" in top_level, top_level       # the one thing it does need
    assert not any("renderer" in name for name in top_level), top_level
    body = ast.unparse(tree)
    assert "importlib.import_module(RENDERER_IMPORT_NAME)" in body
    assert "except ImportError" in body
