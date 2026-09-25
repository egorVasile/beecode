"""`skin-hud`, the pack that owns interface lines, held to what it claims.

The other reference packs draw one animated row. This one takes over four of the
five surfaces a skin may ask for, so the cases here are about *responsibility*:

*  it passes the **full** source gate — `skins.check_source()`, the one that also
   refuses private names — not only the import allow-list. A pack that only ever
   arrives as a file on disk never meets that rule (its folder is trusted instead),
   but a reader who copies this one into a scratch skin does, and three of the
   older packs would be refused there;
*  it **asks for what fits**: on a narrow screen the HUD claim is dropped before a
   strip is ever reserved, and `/skins` says which lines it holds;
*  its markup **renders**: the status line reaches the console as coloured words,
   not as `[` and `#rrggbb`;
*  the HUD is drawn through the **real** painter into a real grid, its bar follows
   the seconds `dt` gave it, and a painter without the newer widgets still gets a
   row rather than a lost surface;
*  and a frame stays inside the budget the host demotes at, because a reference
   that costs 8 ms teaches the next author to spend 8 ms.

The pack is loaded the way a user loads it — the real `PluginLoader`, a real
project folder, a real `plugin.json` — and separately as source, because those are
two different doors and the second one is stricter.
"""
import io
import json
import re
import shutil
import statistics
import sys
import time

import pytest
from rich.console import Console

from beeagent import i18n
from beeagent.config.schema import BeeConfig
from beeagent.core import renderer, skins
from beeagent.core.agent import Agent
from beeagent.plugins.catalog import CATALOG_PATH, TEMPLATES_DIR

PACK = TEMPLATES_DIR / "plugins" / "skin-hud"
SOURCE = (PACK / "plugin.py").read_text(encoding="utf-8")

NAME = "hud"
HOLDING = ("status", "spinner", "thinking", "hud")


def plain(line: str) -> str:
    """What a surface line says once Rich has read its markup.

    A skin's answer is markup, so the words have to be tested after the parse —
    asserting on the raw string would only prove the pack emits tags.
    """
    from rich.text import Text

    return str(Text.from_markup(line))


# ------------------------------------------------------------------ fixtures ---

@pytest.fixture()
def host():
    """An empty skin registry, and an empty one left behind."""
    was = skins.active_name()
    skins.reset()
    skins.set_notifier(lambda text: None)
    yield skins
    skins.set_notifier(None)
    skins.reset()
    if was and was != skins.active_name():
        skins.switch(was)


@pytest.fixture()
def english():
    previous = i18n.get_lang()
    i18n.set_lang("en")
    yield
    i18n.set_lang(previous)


def install():
    """Register the pack as *source*, which is the route the AST gate watches."""
    entry = skins.install_source(NAME, SOURCE)
    assert not entry.refused, [str(r) for r in entry.refusals]
    refusal = skins.switch(NAME)
    assert not refusal, refusal
    return entry


def painter_for(cols: int, rows: int = 1):
    grid = renderer.Grid(cols, rows)
    return grid, renderer.Painter(grid, size=(cols, rows))


def import_pack():
    """The pack as its own fresh module: new state, no shared singleton."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("beeagent_test_skin_hud",
                                                 PACK / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


# ------------------------------------------------------------------- the gate ---

def test_the_pack_passes_the_source_gate_not_only_its_imports():
    """`check_source` is stricter than the loader: it refuses private names too."""
    assert skins.check_source(SOURCE) == []


def test_the_manifest_describes_the_folder_it_sits_in():
    manifest = json.loads((PACK / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "skin-hud" and manifest["entry"] == "plugin.py"
    assert (PACK / "plugin.py").is_file()
    assert re.match(r"^\d+\.\d+\.\d+$", manifest["version"])
    assert len(manifest["description"]) > 20
    assert set(manifest) <= {"name", "version", "type", "description", "entry"}


def test_the_catalog_lists_the_pack_it_ships():
    """`/marketplace` offers the name; the folder has to be the one behind it."""
    from beeagent.plugins.catalog import Catalog

    item = Catalog(CATALOG_PATH).get("skin-hud")
    assert item is not None, "skin-hud is in the tree but not on the shelf"
    assert item.source["kind"] == "builtin"
    assert (TEMPLATES_DIR / item.source["path"] / "plugin.py").is_file()


def test_the_real_loader_takes_the_folder_and_registers_the_skin(tmp_path, monkeypatch,
                                                                host):
    """Not an assumption about the manifest: the loader has to accept the folder."""
    monkeypatch.chdir(tmp_path)
    shutil.copytree(PACK, tmp_path / ".beeagent" / "plugins" / "skin-hud")
    (tmp_path / ".beeagent" / "plugins.json").write_text(
        json.dumps({"installed": {"skin-hud": {"type": "plugin", "enabled": True}}}),
        encoding="utf-8")
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    assert not agent.plugins.load_errors, agent.plugins.load_errors
    assert not agent.plugins.withheld, agent.plugins.withheld
    contributions = [(c.kind, c.name, c.plugin)
                     for c in agent.plugins.extensions.contributions]
    assert ("skin", "skins:" + NAME, "skin-hud") in contributions, contributions
    # The lifecycle, not just the shelf entry: `api.skin_hooks()` has to be the
    # door a pack walks through, or the user installs a skin that never draws.
    record = skins.stats(NAME)
    assert "on_hud" in record["hooks"], record["hooks"]
    assert "on_status" in record["hooks"], record["hooks"]
    assert skins.switch(NAME) == ""
    assert sorted(skins.surfaces()["held"]) == sorted(HOLDING), skins.surfaces()


# ------------------------------------------------------------------- claims ---

def test_it_holds_every_surface_it_asks_for(host):
    install()
    report = skins.surfaces()
    assert sorted(report["held"]) == sorted(HOLDING), report
    assert not report["refused"], report
    assert report["hud_rows"] == 1, report
    assert skins.needs_tick() is True, "a skin with a HUD and a spinner wants a clock"


def test_a_screen_too_narrow_for_a_strip_does_not_get_one(host, monkeypatch):
    """The claim is filtered against the terminal, so nothing is reserved to be empty."""
    monkeypatch.setattr(skins, "_terminal_size", lambda: (32, 10))
    install()
    report = skins.surfaces()
    assert sorted(report["held"]) == ["spinner", "status", "thinking"], report
    assert report["hud_rows"] == 0
    grid, painter = painter_for(32)
    assert skins.hud_frame(painter, 0.1) is False
    assert grid.line(0).strip() == "", repr(grid.line(0))


def test_listing_says_which_lines_belong_to_the_skin(host):
    install()
    listing = skins.listing()
    assert NAME in listing, listing
    row = next(line for line in listing.splitlines() if NAME in line)
    for surface in HOLDING:
        assert surface in row, row


# ---------------------------------------------------------- the text surfaces ---

def test_the_status_line_is_the_skin_s_words_with_the_host_s_numbers(host, english):
    install()
    for _ in range(3):
        skins.post("stream_delta", {"text": "so"})
    line = skins.status_text("model gpt-4o")
    assert "writing" in line, line
    assert "3t" in line, line
    assert line.endswith("model gpt-4o"), line


def test_the_status_line_reaches_the_console_as_colour_not_letters(host, english,
                                                                  monkeypatch):
    """Markup the host does not parse is a status line reading `[#ffcc00 bold]`."""
    from beeagent.ui.components import ResponseStream

    install()
    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=70, force_terminal=False))
    ResponseStream().on_status()
    printed = buffer.getvalue()
    assert "[#" not in printed and "[/]" not in printed, printed
    assert "idle" in printed, printed


def test_the_spinner_keeps_the_phrase_and_walks_on_the_clock(host, english):
    install()
    first = skins.spinner_text("buzzing over the keyboard...", 0.0)
    second = skins.spinner_text("buzzing over the keyboard...", 0.8)
    assert plain(first) == "buzzing over the keyboard... ", repr(first)
    assert first != second, "the clock never reached the line"


def test_a_spinner_with_nothing_to_say_leaves_the_line_empty(host):
    install()
    assert skins.spinner_text("", 1.0) == ""


def test_the_thinking_line_stays_plain_text(host):
    """The reasoning block is the model's writing: a bracket there is a bracket.

    The host prints these lines inside its own `Text`, so markup would show up as
    letters, and a skin that thought it was styling them would be decorating the
    screen with its own source.
    """
    install()
    line = skins.thinking_text("if a[i] > b[0]: pass")
    assert "[i]" in line and "[0]" in line, line


# ---------------------------------------------------------------- the strip ---

def test_the_hud_names_the_tool_and_a_bar_that_grows(host, english):
    install()
    grid, painter = painter_for(80)
    skins.post("tool_start", {"tool": "read_file"})
    for _ in range(3):
        skins.frame(painter, 1.0)
    assert skins.hud_frame(painter, 0.1) is True
    early = grid.line(0)
    assert "read_file" in early and "running" in early, early

    later, second = painter_for(80)
    install()
    skins.post("tool_start", {"tool": "read_file"})
    for _ in range(14):
        skins.frame(second, 1.0)
    skins.hud_frame(second, 0.1)
    late = later.line(0)
    assert late.count("#") > early.count("#"), (early, late)


def test_the_hud_counts_tokens_and_says_the_rate(host, english):
    install()
    grid, painter = painter_for(80)
    for _ in range(10):
        skins.post("stream_delta", {"text": "so"})
        skins.frame(painter, 1.0)
    skins.hud_frame(painter, 0.1)
    row = grid.line(0)
    assert "10t" in row and "1/s" in row, row


def test_the_strip_draws_nothing_past_its_own_row(host, english):
    """`HUD_ROWS` is a promise about height as well as a reservation."""
    install()
    grid, painter = painter_for(80, rows=3)
    skins.post("stream_delta", {"text": "hi"})
    skins.frame(painter, 1.0)
    skins.hud_frame(painter, 0.1)
    assert grid.line(0).strip(), "the row is empty"
    assert grid.line(1).strip() == "" and grid.line(2).strip() == ""


class OldPainter:
    """The six documented methods and no more: a bar is a later addition."""

    def __init__(self, grid):
        self.grid = grid
        self.text = []

    def draw_text(self, x, y, text, color=None, bg=None, style=None):
        self.text.append(text)
        return self.grid.blit(x, y, text)[0] if hasattr(self.grid, "blit") else len(text)

    def draw_box(self, *args, **kwargs):
        return None

    def clear_region(self, x, y, w, h):
        return None

    def get_terminal_size(self):
        return (80, 1)

    def color_support(self):
        return "16"

    def color(self, value):
        return value


def test_an_old_painter_still_gets_a_row(host, english):
    """The bars and tickers came after the six documented methods.

    A pack that called a widget the host does not have would raise, and the engine
    would report that as "this skin lost the hud surface" — a version difference
    reads as a broken skin. So the fallback has to draw, not merely survive.
    """
    install()
    grid = renderer.Grid(80, 1)
    old = OldPainter(grid)
    skins.post("stream_delta", {"text": "hi"})
    skins.frame(old, 8.0)
    assert skins.hud_frame(old, 0.1) is True
    assert skins.owns("hud") is True, "the strip was taken away for want of a bar"
    assert any("#" in text for text in old.text), old.text
    assert any("writing" in text for text in old.text), old.text


def test_a_typical_hud_frame_costs_far_less_than_the_budget(host, english):
    """The median is the pack's cost; the worst frame on this box is the machine's.

    Measured 2026-09-26 on the machine this was written on: an `on_hud` that does
    nothing spikes past 8 ms there too, with the agent, a test run and an indexer
    sharing the cores — so a test on the worst single frame would fail for a reason
    no skin author can act on. The median is the number this pack owns.
    """
    install()
    grid, painter = painter_for(120)
    skins.post("tool_start", {"tool": "bash"})
    budget = skins.budget()["frame_ms"]
    frames = []
    for _ in range(60):
        skins.frame(painter, 0.08)
        started = time.perf_counter()
        skins.hud_frame(painter, 0.08)
        frames.append((time.perf_counter() - started) * 1000.0)
    typical = statistics.median(frames)
    record = skins.stats(NAME)
    assert typical < budget / 4, "median HUD frame %.2f ms against a %.0f ms budget" % (
        typical, budget)
    assert not record["demoted"], record["reason"]
    assert not record["dropped"], "%s (worst frame %.2f ms)" % (record["dropped"],
                                                                max(frames))
    assert not painter.frame_warnings, painter.frame_warnings


def test_the_hud_is_readable_on_a_terminal_that_cannot_do_colour(host, english):
    """The row is text first: with colour gone the words and the bar still say it."""
    install()
    grid = renderer.Grid(80, 1)
    painter = renderer.Painter(grid, size=(80, 1), depth="16")
    skins.post("stream_delta", {"text": "hello"})
    skins.frame(painter, 2.0)
    skins.hud_frame(painter, 0.08)
    row = grid.line(0)
    assert "writing" in row and "#" in row, row


# ------------------------------------------------------------ two languages ---

def test_both_languages_come_from_the_pack_not_an_import():
    """A pack may not import `beeagent.i18n`; the translator arrives on the context."""
    class Ctx:
        language = "ru"

        @staticmethod
        def size():
            return (80, 24)

        @staticmethod
        def translate(english, russian):
            return russian

    mod = import_pack()
    mod.on_init(Ctx())
    assert mod.on_status("") == "[#ffcc00 bold]в покое[/]", mod.on_status("")
    mod.on_event("tool_start", {"tool": "grep"})
    assert "выполняю" in mod.on_status(""), mod.on_status("")
    assert plain(mod.on_spinner("host phrase", 0.0)) == "host phrase "


def test_the_pack_hands_its_hooks_to_the_engine(host):
    """`setup()` on today's host: the engine gets the lifecycle, and can drive it."""
    class Bare:
        def __init__(self):
            self.given = []

        def skin(self, slot, name, value=None):
            self.given.append((slot, name))

    mod = import_pack()
    api = Bare()
    mod.setup(api)
    assert NAME in skins.names(), (
        f"the pack's setup() registered nothing the user can switch to: {skins.names()}")
    assert ("skins", NAME) in api.given, api.given
    record = skins.stats(NAME)
    assert not record["refused"], record["refused"]
    assert set(record["hooks"]) == set(mod.hooks()), (
        "the engine found %s of the pack's hooks; a dict handed to register() is "
        "inert unless the host reads it as one" % sorted(record["hooks"]))
    assert skins.switch(NAME) == ""
    assert sorted(skins.surfaces()["held"]) == sorted(HOLDING), skins.surfaces()


def test_the_pack_falls_back_to_the_event_bus_when_no_host_takes_hooks(monkeypatch):
    """An engine with no door for lifecycle hooks: the pack still tracks state."""
    def refused(*args, **kwargs):
        raise AttributeError("this host has no register()")

    class BusOnly:
        def __init__(self):
            self.events = []

        def event(self, name, callback):
            self.events.append((name, callback))

    monkeypatch.setattr(skins, "register", refused)
    mod = import_pack()
    bus = BusOnly()
    mod.setup(bus)
    assert {name for name, _ in bus.events} >= {"stream_delta", "tool_start", "done"}
    assert all(callback is mod.on_event for _, callback in bus.events)
