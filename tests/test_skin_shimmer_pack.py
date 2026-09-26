"""`skin-shimmer` — the skin that draws the answer, checked as a renderer.

A skin that takes over the biggest block on screen can fail in ways a status-line
skin cannot, so these cases are not "did it paint":

* **no bytes lost.** The answer is the model's text; a restyling renderer that
  swallows a line, a bullet or a code block is a bug nobody sees until the day a
  diff goes missing. Every layout path here is asserted against its input;
* **the cost is bounded and measured.** The live path draws at most `LIVE_TAIL`
  lines however long the answer has grown, a laid-out block is cached against its
  text, and the cache itself is capped — `CACHE_MAX`, not "however many answers
  this session had";
* **the two doors both open.** The classic REPL is reached through the `frame`,
  `banner` and `stream` slots (and the test checks they are *chosen*, not merely
  registered); the TUI is reached through the `answer` surface, whose layout pass
  the host runs at one quarter of the frame rate;
* **it degrades, in both directions.** No colour → the same text with no escape
  sequences; no widget on the painter → the HUD still draws; narrow screen → no
  frame rather than a frame on top of the text.

The pack arrives the way a user installs it: the real `PluginLoader`, a real
project folder, a real `plugin.json`. It is a plugin pack, not a source skin, and
it imports Rich — which the source gate would refuse, and which is exactly why the
answer surface also accepts markup text from a skin that cannot import anything.
"""
import io
import json
import shutil
import sys
import time
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

from beeagent import i18n
from beeagent.config.schema import BeeConfig
from beeagent.core import renderer, skins
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.plugins.catalog import CATALOG_PATH, TEMPLATES_DIR
from beeagent.ui import skin as ui_skin

ROOT = Path(__file__).resolve().parent.parent
PACK = TEMPLATES_DIR / "plugins" / "skin-shimmer"
NAME = "shimmer"

ANSWER = """# Итог

Разобрался:

- первый шаг: прочитать `beeagent.json`
- второй — проверить рамку
  - вложенный пункт
1. нумерованный
2. второй номер

```python
def f(x):
    return x[0]
```

Обычный абзац с **жирным** и `кодом`.
"""


# ------------------------------------------------------------------ fixtures ---

@pytest.fixture()
def host():
    was = skins.active_name()
    skins.reset()
    skins.set_notifier(lambda text: None)
    yield skins
    skins.set_notifier(None)
    skins.reset()
    ui_skin.reset()
    if was and was != skins.active_name():
        skins.switch(was)


@pytest.fixture()
def english():
    previous = i18n.get_lang()
    i18n.set_lang("en")
    yield
    i18n.set_lang(previous)


def import_pack():
    import importlib.util

    spec = importlib.util.spec_from_file_location("beeagent_test_skin_shimmer",
                                                 PACK / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def use_pack():
    """Register the pack as the host would and put it on screen."""
    mod = import_pack()
    skins.register(mod.NAME, mod, description="test")
    assert skins.switch(mod.NAME) == ""
    return mod


def printed(block, width: int = 64, colors: bool = False) -> str:
    cons = Console(file=io.StringIO(), width=width,
                   color_system="truecolor" if colors else None,
                   force_terminal=colors, no_color=not colors,
                   highlight=False, markup=False)
    cons.print(block)
    return cons.file.getvalue()


# ------------------------------------------------------------------ the block ---

def test_the_block_says_every_word_the_model_said(english):
    """The whole point, and the whole risk: a prettier renderer must not lie."""
    mod = use_pack()
    out = printed(mod.on_answer(ANSWER, True))
    for fragment in ("Итог", "первый шаг", "beeagent.json", "второй", "вложенный",
                     "нумерованный", "второй номер", "def f(x):", "return x[0]",
                     "жирным", "кодом"):
        assert fragment in out, "lost %r:\n%s" % (fragment, out)
    assert "#" not in out and "```" not in out, "the markdown leaked as syntax"


def test_the_answer_arrives_inside_an_outline(english):
    mod = use_pack()
    out = printed(mod.on_answer(ANSWER, True))
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[0].startswith("┌") and lines[0].endswith("┐"), lines[0]
    assert lines[-1].startswith("└") and lines[-1].endswith("┘"), lines[-1]
    assert all(line.startswith("▏") and line.endswith("▏") for line in lines[1:-1]), \
        "\n".join(lines[1:-1])
    assert "BeeCode" in lines[0], "the outline carries no name to say whose it is"


def test_the_outline_walks_the_ramp_instead_of_being_one_colour(english):
    mod = use_pack()
    painted = printed(mod.on_answer(ANSWER, True), width=64, colors=True)
    hexes = {token for token in painted.split("m") if token.startswith("\x1b[38;2;")}
    assert len(hexes) > 8, "only %d colours on a frame meant to shimmer" % len(hexes)
    assert "\x1b[38;2;255;204;0m" in painted, "no honey"      # #ffcc00
    assert "\x1b[38;2;124;179;66m" in painted, "no leaf"      # #7cb342


def test_the_list_markers_walk_the_ramp_instead_of_repeating_one_colour(english):
    """The ramp spans the list once: that is what makes a list read as one thought."""
    mod = import_pack()
    mod.shimmer.open_list(5)
    offsets = [mod.shimmer.next_marker() for _ in range(5)]
    assert offsets[0] == 0 and len(set(offsets)) == 5, offsets
    assert offsets == sorted(offsets) and offsets[-1] <= mod.CYCLE - 1, offsets
    mod.shimmer.open_list(1)
    assert mod.shimmer.next_marker() == 0
    mod.shimmer.open_list(200)          # longer than the ramp: still monotonic
    wide = [mod.shimmer.next_marker() for _ in range(200)]
    assert wide == sorted(wide) and wide[-1] <= mod.CYCLE - 1, wide[-3:]
    painted = printed(mod.on_answer(ANSWER, True), width=64, colors=True)
    assert painted.count("\x1b[38;2;255;204;0m") >= 2, "no honey bullet anywhere"
    assert len({token for token in painted.split("m") if token.startswith("\x1b[38;2")}) > 6, \
        "the answer was painted in one colour"


def test_a_terminal_without_colour_gets_the_same_text_without_escapes(english):
    mod = use_pack()
    mod.shimmer.colourful = False
    plain = printed(mod.on_answer(ANSWER, True))
    assert "\x1b[" not in plain
    assert "первый шаг" in plain and "return x[0]" in plain, plain
    assert "Итог" in plain


def test_a_dash_inside_a_code_block_stays_the_model_s_text(english, monkeypatch):
    """`- ` is a bullet in prose and a line of code inside a fence.

    The skin restyles markers, so it has to know which one it is looking at — and
    the host's own fence flag is already back to false by the time a closed block
    is printed, which is how this bug would arrive.
    """
    from beeagent.ui import components

    code = "list:\n```\n- это код\n```\n- это пункт\n"
    mod = import_pack()
    cheap = printed(mod.lines_of(code, 0, True), width=40)
    assert "- это код" in cheap, cheap
    assert "• это пункт" in cheap, cheap

    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=60, color_system=None))
    stream = mod.rail_stream()()
    stream.on_content(code)
    stream.on_done()
    out = buffer.getvalue()
    assert "- это код" in out, out
    assert "• это пункт" in out, out
    assert "• это код" not in out, out


def test_a_narrow_screen_is_left_the_text_width(english):
    """Below the frame's own minimum the rails would eat the answer's letters."""
    mod = use_pack()
    out = printed(mod.on_answer("text - one\n- two\n", True), width=16)
    assert "┌" not in out, out
    assert "one" in out and "two" in out, out


# ------------------------------------------------------------- the live path ----

def test_while_it_arrives_the_answer_is_the_tail_and_nothing_else(english):
    """The cost of a streaming frame must not grow with the answer.

    Forty pages re-typeset twelve times a second is how a skin that looks free
    ends up eating the terminal; drawing the last lines the person can actually
    see is both cheaper and what the live area shows.
    """
    mod = use_pack()
    huge = "".join("line %d - item\n" % n for n in range(4000))
    live = mod.on_answer(huge, False)
    assert len(live.split("\n")) <= mod.LIVE_TAIL + 2, len(live.split("\n"))
    assert "line 3999 - item" in live, "the tail is not the tail"
    assert "line 0 - item" not in live
    rows = [line for line in str(live).split("\n") if line]
    assert all(line.startswith(mod.RAIL) for line in rows), \
        "a line of the live answer reached the screen with no rail:\n%s" % rows[:4]
    finished = printed(mod.on_answer("short answer\n- a\n", True), width=40)
    assert "short answer" in finished


def test_a_laid_out_block_is_built_once_and_the_cache_is_bounded(english):
    mod = use_pack()
    first = mod.on_answer(ANSWER, True)
    assert mod.on_answer(ANSWER, True) is first, "the same text was laid out twice"
    for n in range(40):
        mod.on_answer(ANSWER + "\n%d" % n, True)
    assert len(mod.shimmer._blocks) <= mod.CACHE_MAX, len(mod.shimmer._blocks)
    assert len(mod.shimmer._order) <= mod.CACHE_MAX


def test_a_frame_of_the_skin_costs_less_than_the_host_demotes_at(english):
    """Measured, not asserted: the numbers are printed so a change is visible."""
    mod = use_pack()
    budget = skins.budget()["event_ms"]
    huge = "".join("line %d - item\n" % n for n in range(4000))
    times = []
    for _ in range(30):
        started = time.perf_counter()
        mod.on_answer(huge, False)
        times.append((time.perf_counter() - started) * 1000.0)
    times.sort()
    typical, p90 = times[len(times) // 2], times[int(len(times) * 0.9)]
    print("skin-shimmer live frame: median %.3f ms | p90 %.3f | worst %.3f ms"
          % (typical, p90, times[-1]))
    assert typical < budget / 4, "median live frame %.2f ms" % typical
    assert p90 < budget, "p90 live frame %.2f ms against a %.0f ms budget" % (p90, budget)


# ------------------------------------------------------------ the classic doors ---

class Recorder:
    """A stand-in extension API that records what a pack asks of the interface."""

    def __init__(self, verbs=("skin_hooks",)):
        self.offered = []
        self.chosen = []
        self.given = None
        for verb in verbs:
            setattr(self, verb, self._take)

    def _take(self, name, hooks, description=""):
        self.given = (name, sorted(hooks))
        return True

    def skin(self, slot, name, value=None):
        self.offered.append((slot, name, value))
        return True

    def set_skin(self, slot, name):
        self.chosen.append((slot, name))
        return True

    def event(self, name, callback):
        return None


def test_the_pack_asks_the_classic_interface_to_use_its_variants():
    mod = import_pack()
    api = Recorder()
    mod.setup(api)
    slots = {slot for slot, _name, _value in api.offered}
    assert {"frame", "banner", "stream", "skins"} <= slots, api.offered
    assert (NAME, api.given[0]) and "on_answer" in api.given[1], api.given
    # Registered but not chosen would change nothing on screen.
    assert set(api.chosen) == {("frame", NAME), ("banner", NAME), ("stream", NAME)}, \
        api.chosen
    # The waiting line is a surface, not a slot: claiming both would hide one.
    assert ("spinner", NAME) not in api.chosen, api.chosen


def test_the_frame_variant_is_what_a_panel_asks_for():
    """A frame dict is the shape `ui.skin.frame_kwargs()` spreads into a Panel."""
    mod = import_pack()
    api = Recorder()
    mod.setup(api)
    given = [value for slot, _name, value in api.offered if slot == "frame"]
    assert len(given) == 1 and isinstance(given[0], dict), given
    assert given[0].get("box") is not None, given
    assert "border_style" in given[0], given
    ui_skin.register("frame", NAME, given[0])
    assert ui_skin.choose("frame", NAME, source="test")
    assert ui_skin.frame_kwargs() == given[0], "a panel would not wear this frame"


def test_the_stream_variant_replaces_the_answer_printer_and_keeps_its_rules(
        host, english, monkeypatch):
    """The rail is ours; the buffering is the host's, and must stay the host's.

    A subclass that reimplemented `_print` from scratch would be a second answer
    renderer to keep in sync, and the rules it would silently drop are the ones
    this program bled over: whole lines only while the prompt owns the screen, a
    partial line held back, the buffer flushed on a boundary.
    """
    from beeagent.ui import components

    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=60, force_terminal=False))
    mod = import_pack()
    api = Recorder()
    mod.setup(api)
    cls = [value for slot, _name, value in api.offered if slot == "stream"][0]
    assert isinstance(cls, type) and issubclass(cls, components.ResponseStream)

    stream = cls()
    stream.on_content("одна строка\n")
    assert "вторая" not in buffer.getvalue()
    stream.on_content("вторая ")          # no newline yet: nothing may reach the screen
    assert "вторая" not in buffer.getvalue(), "a partial line was written mid-line"
    stream.on_content("строка\n")
    stream.on_done()
    printed_text = buffer.getvalue()
    assert "одна строка" in printed_text and "вторая строка" in printed_text
    assert "▏" in printed_text, "the rail is missing from the streamed line"
    assert cls is not components.ResponseStream, "the slot registered the built-in class"


# ------------------------------------------------------------------ the TUI ---

def app_running(tmp_path):
    from beeagent.ui.tui import BeeCodeApp

    app = BeeCodeApp(config=BeeConfig(), session=Session())
    app.agent.workdir = str(tmp_path)
    return app


def test_the_tui_prints_the_answer_the_skin_laid_out(tmp_path, host, monkeypatch):
    """Not 'the hook was called': the block has to reach the log a person reads."""
    import asyncio

    mod = use_pack()
    app = app_running(tmp_path)

    async def scenario():
        from beeagent.ui.tui import BeeCodeApp

        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            writes = []
            original = app.chatlog.write
            app.chatlog.write = lambda renderable, *a, **k: (
                writes.append(renderable), original(renderable, *a, **k))
            app._on_agent_event("stream_delta", {"text": "текст ответа\n- пункт\n"})
            app._on_agent_event("done", {"text": "текст ответа\n- пункт\n"})
            await pilot.pause(0.2)
            block = [w for w in writes if type(w).__name__ == "Framed"]
            assert block, "the answer went to the log without the skin's frame: %r" % (
                [type(w).__name__ for w in writes])
            assert app._waiting_line is False
    asyncio.run(scenario())


def test_the_live_answer_is_laid_out_on_the_rhythm_not_every_frame(tmp_path, host):
    """The host's own half of the cost rule: 12 fps for the lines, ~3 for a block."""
    import asyncio

    mod = use_pack()
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            calls = []
            real = skins.answer_render

            def counting(text, final=False):
                if not final:
                    calls.append(text)
                return real(text, final)

            skins.answer_render = counting
            app._stop_skin_clock()          # drive the frames ourselves, or the
            app._answer_beats = 0           # app's own clock joins in and no count
            try:                            # means anything
                for frame in range(app.ANSWER_BEATS * 3):
                    app._on_agent_event("stream_delta",
                                        {"text": "с%d " % frame})
                    app._skin_tick()
                assert len(calls) == 3, (
                    "laid out %d times in %d frames; ANSWER_BEATS=%d promised one "
                    "layout every %d" % (len(calls), app.ANSWER_BEATS * 3,
                                         app.ANSWER_BEATS, app.ANSWER_BEATS))
                # And a frame in which nothing arrived must not lay anything out
                # again, however many beats go by.
                before = len(calls)
                for _ in range(app.ANSWER_BEATS * 2):
                    app._skin_tick()
                assert len(calls) == before, "an unchanged answer was re-typeset"
            finally:
                skins.answer_render = real
    asyncio.run(scenario())


# ------------------------------------------------------------------ the pack ----

def test_the_pack_loads_through_the_real_loader_and_holds_the_surfaces(tmp_path,
                                                                      monkeypatch,
                                                                      host):
    monkeypatch.chdir(tmp_path)
    shutil.copytree(PACK, tmp_path / ".beeagent" / "plugins" / "skin-shimmer")
    (tmp_path / ".beeagent" / "plugins.json").write_text(
        json.dumps({"installed": {"skin-shimmer": {"type": "plugin",
                                                   "enabled": True}}}),
        encoding="utf-8")
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    assert not agent.plugins.load_errors, agent.plugins.load_errors
    assert not agent.plugins.withheld, agent.plugins.withheld
    record = skins.stats(NAME)
    assert "on_answer" in record["hooks"], record["hooks"]
    assert skins.switch(NAME) == ""
    held = skins.surfaces()["held"]
    assert held == ["status", "spinner", "thinking", "answer", "hud"], held
    assert skins.surfaces()["hud_rows"] == 2, skins.surfaces()
    assert skins.needs_tick() is True


def test_the_manifest_and_the_catalog_row_name_the_same_folder():
    manifest = json.loads((PACK / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "skin-shimmer" and manifest["entry"] == "plugin.py"
    assert len(manifest["description"]) > 20
    from beeagent.plugins.catalog import Catalog

    item = Catalog(CATALOG_PATH).get("skin-shimmer")
    assert item is not None and item.source["path"] == "plugins/skin-shimmer"


def test_the_hud_keeps_its_two_rows_useful_and_survives_an_old_painter(english):
    mod = use_pack()
    # The first event after a switch is the one a busy machine can make expensive
    # enough for the host to stop the skin outright, and this test is about what
    # the strip draws, not about that policy (which test_skin_surfaces.py covers).
    skins.post("nudged", {})
    grid = renderer.Grid(70, 2)
    painter = renderer.Painter(grid, size=(70, 2), depth="truecolor")
    skins.post("tool_start", {"tool": "read_file"})
    for _ in range(5):
        skins.post("stream_delta", {"text": "so"})
        mod.on_frame(1.0, painter)
    assert skins.hud_frame(painter, 0.08) is True
    top, bottom = grid.line(0), grid.line(1)
    # The token arrived after the call started, so the line says "writing" and
    # still names the tool: the state word is the newest event, the tool is what
    # is occupying the screen.
    assert "writing" in top or "пишу" in top, top
    assert "read_file" in bottom, bottom
    assert top.count("#") >= 1, top
    assert not painter.frame_warnings, painter.frame_warnings

    class TextOnly:
        def __init__(self):
            self.rows = {}

        def draw_text(self, x, y, text, color=None, bg=None, style=None):
            self.rows.setdefault(y, "")
            self.rows[y] = self.rows[y][:x] + text
            return len(text)

        def clear_region(self, x, y, w, h):
            return None

        def get_terminal_size(self):
            return (70, 2)

    old = TextOnly()
    assert skins.hud_frame(old, 0.08) is True
    assert skins.owns("hud") is True, "the strip was dropped for want of a bar"
    assert "#" in old.rows.get(0, "") or "." in old.rows.get(0, ""), old.rows


def test_both_languages_are_in_the_file_and_the_title_follows_the_user():
    class Ctx:
        language = "ru"

        @staticmethod
        def size():
            return (90, 30)

        @staticmethod
        def translate(english, russian):
            return russian

    mod = import_pack()
    mod.on_init(Ctx())
    titled = printed(mod.on_answer("текст\n", True), width=40)
    assert "пчела" in titled, titled
    assert "ответил" in mod.on_status("") or "в покое" in mod.on_status("")
