"""Tests for `beeagent/core/renderer.py`, driven through a fake TTY.

Everything here talks to an `io.StringIO` that claims `isatty()`, with the size
patched, so the assertions are about *bytes on the wire*: the diff is proven by
counting what the stream received, not by trusting the counters. Two numbers this
suite exists to report are measured in `test_measured_cost...` — the cost of a
full 80x24 repaint and of a one-cell diff on the machine running them.

The language is left at the default (`en`) because `frame_warnings` text is what
some assertions look for; `conftest.py` already keeps the setting from leaking.
"""
import io
import threading
import time

import pytest

from beeagent.core import renderer as R
from beeagent.core.renderer import (
    BLANK, Emitter, FrameLoop, Grid, Painter, Screen, clamp_fps, clamp_size,
    color_support, degrade, detect_color_support, make_cell, nearest_16, nearest_256,
    sgr, strip_terminal, terminal_size, text_cells,
)

ESC = "\x1b"


class FakeTTY(io.StringIO):
    """A stream that says it is a terminal. `io.StringIO` has no `.buffer`, which
    is exactly the path a test wants: writes stay as text we can assert on."""

    def __init__(self, tty=True):
        super().__init__()
        self._tty = tty

    def isatty(self):
        return self._tty

    @property
    def bytes(self):
        return len(self.getvalue().encode("utf-8"))


@pytest.fixture(autouse=True)
def _restore_depth_cache():
    """`color_support()` is a module global; a test that refreshed it must not leave it."""
    saved = R._support_cache
    yield
    R._support_cache = saved


def painter_of(cols=80, rows=24, depth="truecolor"):
    grid = Grid(cols, rows)
    return Painter(grid, size=(cols, rows), depth=depth), grid


def paint_static_box(painter, grid):
    painter.draw_box(1, 1, 20, 6, border="honey", title="BeeCode")
    painter.draw_text(3, 3, "привет", color=(255, 204, 0), style="bold")


# ---------------------------------------------------------------------------
# 1. The diff proof
# ---------------------------------------------------------------------------

def test_the_same_box_twice_writes_nothing_the_second_time():
    """The engine's whole claim: a frame that looks the same costs zero bytes."""
    painter, grid = painter_of()
    prev = Grid(80, 24)
    emitter = Emitter(None, "truecolor")
    paint_static_box(painter, grid)

    first = emitter.render(prev, grid, full=True)
    assert first["bytes"] > 0 and first["payload"]
    prev.copy_from(grid)

    # The skin paints the identical frame again (this is what a chat UI does while
    # nobody types), and the wire must stay quiet.
    paint_static_box(painter, grid)
    second = emitter.render(prev, grid)
    assert second["payload"] == ""
    assert second["bytes"] == 0
    assert second["runs"] == 0 and second["moves"] == 0 and second["cells"] == 0


def test_the_diff_is_proven_at_the_stream_too():
    """Count bytes written to a fake TTY through the loop, not just the emitter."""
    stream = FakeTTY()
    marks = []

    def paint(p):
        marks.append(stream.bytes)
        p.draw_box(0, 0, 30, 8, border="leaf")

    loop = FrameLoop(paint, stream=stream, size=(80, 24), depth="truecolor")
    loop.run(fps=30, max_frames=4)
    # What the stream got is the box once, plus the lifecycle sequences around it.
    lifecycle = len((Screen.ALT_ENTER + Screen.RESET + Screen.CLEAR + Screen.CURSOR_OFF
                     + Screen.RESET + Screen.CURSOR_ON + Screen.ALT_LEAVE).encode("utf-8"))
    assert stream.bytes == loop.bytes_written + lifecycle
    # Frame 0 put the box on the wire; frames 1..3 added nothing.
    assert marks[1] == marks[2] == marks[3] > marks[0]
    assert loop.emitted_frames == 1, "a steady screen must not be reported as output"


def test_one_changed_cell_is_one_cursor_move_and_one_cell_write():
    """The cheapest possible update: no clear, no row rewrite, no wasted bytes."""
    prev = Grid(80, 24)
    cur = Grid(80, 24)
    cur.put(5, 5, make_cell("X"))
    result = Emitter(None, "truecolor").render(prev, cur)
    assert result["payload"] == ESC + "[6;6HX"
    assert result["moves"] == 1 and result["runs"] == 1 and result["cells"] == 1
    assert result["bytes"] == len(result["payload"].encode("utf-8")) == 7


def test_a_second_run_on_the_same_row_jumps_instead_of_repositioning():
    """Gap handling: within a row of single-width cells, a relative move is cheaper
    than an absolute one, and that is the only reason it is used."""
    prev = Grid(80, 24)
    cur = Grid(80, 24)
    cur.put(2, 7, make_cell("A"))
    cur.put(9, 7, make_cell("B"))
    result = Emitter(None, "truecolor").render(prev, cur)
    assert result["payload"] == ESC + "[8;3HA" + ESC + "[6C" + "B"
    assert result["moves"] == 2
    assert ESC + "[8;10H" not in result["payload"]


def test_an_attribute_change_counts_as_a_change_even_with_the_same_character():
    prev = Grid(10, 5)
    cur = Grid(10, 5)
    prev.put(3, 2, make_cell("x", "x256:220"))
    cur.put(3, 2, make_cell("x", "x16:11"))
    result = Emitter(None, "256").render(prev, cur)
    assert result["cells"] == 1
    # The new value is a basic colour, so it goes out as 93 and the user's theme
    # decides what "bright yellow" means on this terminal.
    assert result["payload"] == ESC + "[3;4H" + ESC + "[93m" + "x" + ESC + "[0m"


# ---------------------------------------------------------------------------
# 2. Colour degradation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ((255, 204, 0), "#ffcc00"),      # honey, exact: the terminal can show it
    ((124, 179, 66), "#7cb342"),     # leaf
    ((255, 255, 255), "#ffffff"),
])
def test_truecolour_keeps_truecolour(value, expected):
    assert degrade(value, "truecolor") == expected


@pytest.mark.parametrize("value,expected", [
    ((255, 204, 0), "x256:220"),     # #ffd700, the cube colour nearest honey
    ((124, 179, 66), "x256:107"),
    ((255, 255, 255), "x256:15"),    # ties go low, and 15 follows the user theme
])
def test_truecolour_degrades_to_the_nearest_of_256(value, expected):
    assert degrade(value, "256") == expected


@pytest.mark.parametrize("value,expected", [
    ((255, 204, 0), "x16:11"),       # bright yellow
    ((124, 179, 66), "x16:8"),
    ((255, 255, 255), "x16:15"),
])
def test_truecolour_degrades_to_the_nearest_of_16(value, expected):
    assert degrade(value, "16") == expected


def test_degradation_is_deterministic_and_idempotent():
    for depth in R.DEPTHS:
        first = degrade((255, 204, 0), depth)
        for _ in range(5):
            assert degrade((255, 204, 0), depth) == first
        assert degrade(first, depth) == first, "a token must survive being re-coloured"


def test_a_named_basic_colour_stays_basic_at_every_depth():
    """`red` is the user's red, not our RGB guess of it — so no depth can degrade it."""
    for depth in R.DEPTHS:
        assert degrade("red", depth) == "x16:1"
    assert sgr("x16:1", None, frozenset(), "truecolor") == ESC + "[31m"


def test_the_sgr_we_emit_matches_the_depth():
    assert sgr("#ffcc00", None, frozenset(), "truecolor") == ESC + "[38;2;255;204;0m"
    assert sgr("#ffcc00", None, frozenset(), "256") == ESC + "[38;5;220m"
    assert sgr("#ffcc00", None, frozenset(), "16") == ESC + "[93m"
    assert sgr(None, "#ffcc00", frozenset(["bold"]), "256") == ESC + "[1;48;5;220m"


def test_nearest_index_ties_go_to_the_lower_index():
    assert nearest_256((0, 0, 0)) == 0 and nearest_16((0, 0, 0)) == 0
    assert nearest_256((255, 255, 255)) == 15 and nearest_16((255, 255, 255)) == 15


@pytest.mark.parametrize("env,expected", [
    ({"COLORTERM": "truecolor"}, "truecolor"),
    ({"COLORTERM": "24bit"}, "truecolor"),
    ({"WT_SESSION": "1"}, "truecolor"),
    ({"TERM": "xterm-256color"}, "256"),
    ({"TERM": "xterm"}, "256"),
    ({"TERM": "dumb"}, "16"),
    ({"TERM": "ansi"}, "256"),
    ({}, "16"),
])
def test_depth_comes_from_the_environment(env, expected):
    assert detect_color_support(env) == expected


def test_depth_is_cached_until_asked_again(monkeypatch):
    monkeypatch.setenv("COLORTERM", "truecolor")
    monkeypatch.delenv("TERM", raising=False)
    assert color_support(refresh=True) == "truecolor"
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.delenv("COLORTERM")
    assert color_support() == "truecolor", "the cached depth must not drift mid-frame"
    assert color_support(refresh=True) == "16"


def test_painter_degrades_for_the_skin_without_the_skin_noticing():
    values = [(255, 204, 0), "#7cb342", "honey"]
    deep = Painter(Grid(20, 5), size=(20, 5), depth="truecolor")
    dull = Painter(Grid(20, 5), size=(20, 5), depth="16")
    assert [deep.color(v) for v in values] == ["#ffcc00", "#7cb342", "#ffcc00"]
    assert [dull.color(v) for v in values] == ["x16:11", "x16:8", "x16:11"]
    assert dull.color_support() == "16"


# ---------------------------------------------------------------------------
# 3. Cells, not bytes: the honesty the tool layer learned the hard way
# ---------------------------------------------------------------------------

def test_cyrillic_counts_cells_not_bytes():
    painter, grid = painter_of(cols=20)
    text = "привет"
    assert painter.draw_text(0, 0, text) == 6
    assert len(text) == 6 and len(text.encode("utf-8")) == 12
    assert grid.line(0)[:6] == text
    assert text_cells(text) == 6


def test_double_width_glyph_takes_two_cells_and_a_continuation():
    painter, grid = painter_of(cols=10)
    assert painter.draw_text(0, 0, "你好") == 4
    assert grid.cell(0, 0).width == 2
    assert grid.cell(1, 0).ch == "", "the right half is claimed, so nothing can sit on it"
    assert text_cells("你好") == 4


def test_a_combining_mark_rides_in_the_cell_before_it():
    painter, grid = painter_of(cols=10)
    assert painter.draw_text(0, 0, "e\u0301x") == 2
    assert grid.cell(0, 0).ch == "e\u0301" and grid.cell(0, 0).width == 1
    assert grid.cell(1, 0).ch == "x"


def test_erasing_a_wide_glyph_clears_both_columns():
    """One space erases one cell; a glyph that took two needs two, or it stays."""
    prev = Grid(20, 3)
    cur = Grid(20, 3)
    cur.put(4, 1, make_cell("你"))
    cur.put(5, 1, make_cell(""))
    first = Emitter(None, "truecolor").render(prev, cur)
    # Writing the base covers both columns on screen, so the model's continuation
    # column costs no byte here — one run, one glyph.
    assert first["payload"] == ESC + "[2;5H你" and first["runs"] == 1
    prev.copy_from(cur)
    cur.put(4, 1, BLANK)
    cur.put(5, 1, BLANK)
    second = Emitter(None, "truecolor").render(prev, cur)
    assert second["payload"] == ESC + "[2;5H  ", repr(second["payload"])
    assert second["cells"] == 2


def test_a_row_longer_than_the_screen_clips_and_reports_it():
    painter, grid = painter_of(cols=20)
    written = painter.draw_text(0, 0, "x" * 30)
    assert written == 20
    assert grid.line(0) == "x" * 20
    assert any("clip" in warning.lower() for warning in painter.frame_warnings)
    # The same complaint at another row is another complaint, and each is counted.
    painter.draw_text(0, 1, "y" * 30)
    assert len(painter.frame_warnings) == 2
    assert sum(painter.warning_counts.values()) == 2


def test_a_negative_origin_clips_instead_of_raising():
    painter, grid = painter_of(cols=10)
    written = painter.draw_text(-3, 0, "abcdef")
    assert written == 3 and grid.line(0)[:3] == "def" and grid.cell(3, 0).ch == " "


def test_multiline_text_flows_down_rows():
    painter, grid = painter_of(cols=10)
    assert painter.draw_text(1, 0, "ab\ncd") == 4
    assert grid.line(0)[1:3] == "ab" and grid.line(1)[1:3] == "cd"


def test_a_tab_advances_to_the_next_tab_stop():
    painter, grid = painter_of(cols=20)
    assert painter.draw_text(0, 0, "a\tb") == 9
    assert grid.cell(0, 0).ch == "a" and grid.cell(8, 0).ch == "b"
    assert grid.line(0)[:8] == "a" + " " * 7


# ---------------------------------------------------------------------------
# 4. Garbage in, warnings out: a skin cannot kill the UI
# ---------------------------------------------------------------------------

def test_control_characters_in_a_label_never_leave_the_grid():
    """A skin's text is model-adjacent: an OSC 52 in a title must not reach the tty."""
    stream = FakeTTY()
    smuggled = "\x1b]52;c;aGVsbG8=\x07title\x1b[2J"

    def paint(p):
        p.draw_text(0, 0, smuggled)
        p.draw_box(0, 2, 10, 4, border="red", title=smuggled)

    loop = FrameLoop(paint, stream=stream, size=(40, 10), depth="256")
    loop.run(fps=30, max_frames=1)
    out = stream.getvalue()
    assert "]52;" not in out and "\x07" not in out
    assert out.count(ESC + "[2J") == 1, "only our own clear, never the one smuggled in"
    assert "title" in out
    assert any("control characters" in warning for warning in loop.frame_warnings)


@pytest.mark.parametrize("junk", [None, object(), 3.5, "not a colour", "#gggggg",
                                  (999, 0), (-1, -1, -1)])
def test_a_bad_colour_falls_back_and_is_reported(junk):
    painter, grid = painter_of(cols=12)
    assert painter.draw_text(0, 0, "abc", color=junk) == 3
    assert grid.cell(0, 0).fg is None
    if junk is not None:
        assert any("colour" in warning for warning in painter.frame_warnings)
    assert painter.color(junk) == "default"


def test_junk_coordinates_and_geometry_are_clipped_not_raised():
    painter, grid = painter_of(cols=15, rows=5)
    painter.draw_box("x", 1, 6, 4)                 # non-numeric geometry
    painter.draw_box(0, 0, 1, 1)                   # too small to be a box
    painter.draw_box(-4, -4, 100, 100, border="red")  # bigger than the screen
    painter.clear_region(None, None, None, None)
    painter.draw_text("q", "q", "text")
    assert grid.cell(0, 0).ch == "t"        # the junk position fell back to (0, 0)
    assert len(painter.frame_warnings) >= 4
    assert any("non-numeric" in warning for warning in painter.frame_warnings)
    assert any("too small" in warning for warning in painter.frame_warnings)
    assert painter.draw_text(0, 0, 12345) == 5     # not text, converted


def test_a_skin_that_loses_its_grid_does_not_lose_the_screen():
    """Everything off the bottom / right edge is dropped and counted, not raised."""
    painter, grid = painter_of(cols=5, rows=3)
    assert painter.draw_text(0, 0, "hello world") == 5
    assert painter.draw_text(0, 99, "down there") == 0


def test_warnings_are_bounded_and_counted():
    painter, _ = painter_of(cols=10, rows=2)
    for index in range(500):
        painter.draw_text(index, 0, "way too long for this grid")
    assert len(painter.frame_warnings) <= R.MAX_WARNINGS
    assert sum(painter.warning_counts.values()) >= 500


# ---------------------------------------------------------------------------
# 5. Screen lifecycle: idempotent, and always given back
# ---------------------------------------------------------------------------

def test_screen_lifecycle_is_idempotent():
    stream = FakeTTY()
    screen = Screen(stream, size=(80, 24))
    assert screen.enter() and not screen.enter()
    assert stream.getvalue().count(R.Screen.ALT_ENTER) == 1
    assert screen.hide_cursor() and not screen.hide_cursor()
    assert screen.show_cursor() and not screen.show_cursor()
    assert screen.leave() and not screen.leave()
    assert stream.getvalue().count(R.Screen.ALT_LEAVE) == 1
    assert stream.getvalue().count(R.Screen.CURSOR_OFF) == 1
    assert stream.getvalue().count(R.Screen.CURSOR_ON) == 1


def test_a_non_terminal_stream_refuses_every_escape():
    stream = io.StringIO()
    screen = Screen(stream)
    assert not screen.is_tty
    for call in (screen.enter, screen.leave, screen.hide_cursor,
                 screen.show_cursor, screen.clear):
        call()
    assert screen.write(R.Screen.CLEAR) == 0
    assert stream.getvalue() == ""


def test_a_terminal_that_vanished_mid_frame_ends_the_loop_quietly():
    class BrokenTTY(FakeTTY):
        def write(self, text):
            raise OSError("device not reachable")

    loop = FrameLoop(lambda p: p.draw_text(0, 0, "hi"), stream=BrokenTTY(),
                     size=(40, 10))
    stats = loop.run(fps=30, max_frames=50)
    assert stats["frames"] <= 2 and loop.screen.write_errors
    assert any("stopped accepting output" in warning for warning in loop.frame_warnings)


# ---------------------------------------------------------------------------
# 6. The frame loop
# ---------------------------------------------------------------------------

def test_fps_defaults_to_twelve_and_clamps_to_the_range():
    assert clamp_fps(None) == 12.0 and R.DEFAULT_FPS == 12
    assert clamp_fps(60) == 30.0 and clamp_fps(120) == 30.0
    assert clamp_fps(0.2) == 1.0 and clamp_fps(-5) == 12.0
    assert clamp_fps(float("nan")) == 12.0 and clamp_fps("twelve") == 12.0
    assert clamp_fps(20) == 20.0
    loop = FrameLoop(lambda p: p.draw_text(0, 0, "x"), stream=FakeTTY(), size=(20, 5))
    assert loop.run(fps=240, max_frames=1)["fps"] == 30.0
    assert loop.run(fps="nonsense", max_frames=1)["fps"] == 12.0


def test_a_slow_skin_skips_frames_rather_than_queueing_them():
    """Overshooting the budget must not build a backlog of stale frames."""
    budget = 1.0 / 30.0
    slow_for = budget * 3.5

    def slow(p):
        time.sleep(slow_for)
        p.draw_text(0, 0, "tick")

    loop = FrameLoop(slow, stream=FakeTTY(), size=(20, 5))
    stats = loop.run(fps=30, max_frames=5)
    assert stats["frames"] == 5
    assert loop.overrun_frames == 5
    assert loop.skipped_frames >= 5, "3.5 budgets of work must drop at least 2 beats"
    assert stats["skipped"] == loop.skipped_frames
    assert stats["emitted"] <= stats["frames"]
    # The wall clock is not asserted against the budget on purpose: a 117 ms sleep
    # measured 190-290 ms on this machine (Windows timer granularity, not the
    # engine). The counters are the honest proof — five frames of work, that many
    # dropped beats, and no attempt to paint the beats that were dropped.


def test_stop_is_safe_to_call_from_another_thread():
    loop = FrameLoop(lambda p: p.draw_text(0, 0, "tick"), stream=FakeTTY(), size=(20, 5))
    watcher = threading.Timer(0.12, loop.stop)
    watcher.start()
    started = time.monotonic()
    stats = loop.run(fps=30)          # no max_frames: only stop() can end this
    watcher.join()
    assert time.monotonic() - started < 1.0, "stop() must cut the wait, not the beat"
    assert 0 < stats["frames"] < 30
    assert not loop.running and not loop.screen.alt and not loop.screen.cursor_hidden


def test_stop_before_run_ends_the_loop_at_zero():
    loop = FrameLoop(lambda p: p.draw_text(0, 0, "x"), stream=FakeTTY(), size=(20, 5))
    loop.stop()
    loop._stop.clear()               # run() clears it by design: a loop is reusable
    assert loop.run(fps=30, max_frames=1)["frames"] == 1


def test_resize_between_frames_repaints_everything_and_tears_nothing():
    size = {"cols": 80, "rows": 24}
    stream = FakeTTY()
    seen = []

    def paint(p):
        seen.append((p.get_terminal_size(), p.cols, p.rows))
        p.draw_text(0, 0, "ABCDEFGHIJ" * 8, color="honey")
        if len(seen) == 2:
            # The user drags the window while frame 1 is being painted: the engine
            # only finds out at the next beat, and must not splice the two shapes.
            size["cols"], size["rows"] = 30, 4

    loop = FrameLoop(paint, stream=stream, size_fn=lambda: (size["cols"], size["rows"]),
                     depth="truecolor")
    stats = loop.run(fps=30, max_frames=4)
    assert stats["resizes"] == 1
    assert seen[1] == ((80, 24), 80, 24) and seen[2] == ((30, 4), 30, 4)
    assert (loop.grid.cols, loop.grid.rows) == (30, 4)
    out = stream.getvalue()
    last_clear = out.rindex(Screen.CLEAR)
    assert last_clear > out.index(Screen.ALT_ENTER), "the repaint cleared first"
    assert "ABCDEFGHIJ" * 3 in out[last_clear:], "the new shape arrived whole"
    assert "ABCDEFGHIJ" * 4 not in out[last_clear:], "the old shape is not still on it"
    assert loop.grid.line(0) == "ABCDEFGHIJ" * 3


def test_the_alternate_screen_and_cursor_come_back_even_when_the_skin_raises():
    stream = FakeTTY()

    def doomed(p):
        p.draw_text(0, 0, "half a frame")
        raise ValueError("the skin is broken")

    loop = FrameLoop(doomed, stream=stream, size=(40, 10))
    stats = loop.run(fps=30)
    assert stats["paint_errors"] >= loop.max_paint_errors
    assert stats["emitted"] == 0, "a frame that raised mid-paint is never shown"
    assert "half a frame" not in stream.getvalue()
    assert not loop.screen.alt and not loop.screen.cursor_hidden and loop.running is False
    out = stream.getvalue()
    assert out.endswith(R.Screen.RESET + R.Screen.CURSOR_ON + R.Screen.ALT_LEAVE)
    assert any("ValueError" in warning for warning in loop.frame_warnings)


def test_a_keyboard_interrupt_still_gives_the_terminal_back():
    stream = FakeTTY()

    def ctrl_c(p):
        raise KeyboardInterrupt

    loop = FrameLoop(ctrl_c, stream=stream, size=(30, 6))
    loop.run(fps=30, max_frames=1)
    assert stream.getvalue().endswith(R.Screen.CURSOR_ON + R.Screen.ALT_LEAVE)


def test_a_pipe_gets_no_frame_loop_and_says_so_once():
    stream = io.StringIO()
    calls = []
    loop = FrameLoop(lambda p: calls.append(1), stream=stream)
    stats = loop.run(fps=12, max_frames=3)
    assert stream.getvalue() == "" and calls == []
    assert stats["frames"] == 0 and stats["emitted"] == 0
    assert stats["tty"] is False and stats["degraded"]
    assert "terminal" in stats["degraded"]
    assert loop.run(fps=12, max_frames=3)["frames"] == 0


def test_a_loop_without_a_skin_reports_the_fallback():
    loop = FrameLoop(None, stream=FakeTTY(), size=(20, 5))
    stats = loop.run(fps=12)
    assert stats["frames"] == 0 and stats["degraded"]
    assert any("no paint callback" in warning for warning in loop.frame_warnings)


def test_the_loop_hands_the_skin_exactly_the_painter_surface():
    surface = ("draw_text", "draw_box", "clear_region", "get_terminal_size",
               "color_support", "color")
    seen = {}
    returns = {}

    def paint(p):
        for name in surface:
            seen[name] = getattr(p, name)
        returns["draw_text"] = p.draw_text(0, 0, "x")
        returns["draw_box"] = p.draw_box(1, 1, 10, 4, border="honey", fill="ink", title="t")
        returns["clear_region"] = p.clear_region(0, 0, 5, 1)
        returns["get_terminal_size"] = p.get_terminal_size()
        returns["color_support"] = p.color_support()
        returns["color"] = p.color((255, 204, 0))
        assert p.get_terminal_size() == (40, 10) and p.color_support() == "256"

    loop = FrameLoop(paint, stream=FakeTTY(), size=(40, 10), depth="256")
    loop.run(fps=30, max_frames=1)
    assert set(seen) == set(surface)
    assert isinstance(returns["draw_text"], int) and returns["draw_text"] == 1
    assert returns["draw_box"] is None and returns["clear_region"] is None
    assert returns["get_terminal_size"] == (40, 10)
    assert returns["color_support"] == "256"
    assert returns["color"] == "x256:220"
    assert loop.grid.line(0)[:5] == " " * 5, "clear_region blanked the corner"
    assert "t" in loop.grid.line(1), "the title belongs to the top edge"


def test_the_skin_can_read_the_frame_it_is_being_asked_for():
    indexes = []
    loop = FrameLoop(lambda p: indexes.append(p.frame_index), stream=FakeTTY(),
                     size=(20, 5))
    loop.run(fps=30, max_frames=4)
    assert indexes == [0, 1, 2, 3]


def test_size_and_fps_garbage_cannot_break_the_loop():
    loop = FrameLoop(lambda p: p.draw_text(0, 0, "x"), stream=FakeTTY(),
                     size_fn=lambda: (0, -3))
    assert loop.run(fps=30, max_frames=1)["frames"] == 1
    assert (loop.grid.cols, loop.grid.rows) == (1, 1)

    def explode():
        raise RuntimeError("no size for you")

    painter = Painter(size_fn=explode)
    assert painter.get_terminal_size() == R.DEFAULT_SIZE


# ---------------------------------------------------------------------------
# 7. Grid primitives
# ---------------------------------------------------------------------------

def test_blit_reports_what_it_wrote_and_what_it_lost():
    grid = Grid(10, 3)
    written, lost = grid.blit(0, 0, "0123456789ABCD")
    assert (written, lost) == (10, 4)
    assert grid.line(0) == "0123456789"
    assert grid.blit(0, 99, "gone") == (0, 4)
    assert grid.put(99, 0, BLANK) is False and grid.cell(99, 0) is None


def test_fill_and_clear_region_are_clipped_to_the_grid():
    grid = Grid(8, 4)
    assert grid.fill(2, 2, 100, 100, "#") == 12      # rows 2-3, columns 2-7
    assert grid.line(2) == "  " + "#" * 6
    painter = Painter(grid, size=(8, 4))
    painter.clear_region(2, 2, 6, 6)
    assert grid.line(2) == " " * 8
    assert grid.cell(3, 3).blank


def test_copy_from_does_not_alias_rows():
    """If prev shared a row list with the live grid, every diff would be empty."""
    a = Grid(5, 2)
    b = Grid(5, 2)
    b.copy_from(a)
    b.put(0, 0, make_cell("Z"))
    assert a.cell(0, 0).ch == " "


def test_a_wide_run_forces_an_absolute_move_after_it():
    """Once our width arithmetic could be wrong, stop trusting relative jumps."""
    prev = Grid(20, 2)
    cur = Grid(20, 2)
    cur.put(0, 0, make_cell("你"))
    cur.put(1, 0, make_cell(""))
    cur.put(2, 0, make_cell("x"))
    result = Emitter(None, "truecolor").render(prev, cur)
    assert result["payload"].count("x") == 1
    assert ESC + "[1;3H" in result["payload"], result["payload"]
    assert ESC + "[1C" not in result["payload"]


# ---------------------------------------------------------------------------
# 8. The measured cost, reported rather than assumed
# ---------------------------------------------------------------------------

def test_measured_cost_of_a_full_repaint_and_a_one_cell_diff_at_80x24():
    """Numbers from this machine, because a claim about speed we cannot measure is
    decoration. Full repaint = every cell of 80x24 with colour; diff = one cell."""
    cols, rows = 80, 24
    stream = FakeTTY()
    screen = Screen(stream, size=(cols, rows))
    emitter = Emitter(screen, "truecolor")
    grid, prev = Grid(cols, rows), Grid(cols, rows)
    painter = Painter(grid, size=(cols, rows), depth="truecolor")

    for y in range(rows):
        painter.draw_text(0, y, "%-79d" % y, color="honey", bg="ink", style="bold")
    # warm up the interning and the SGR cache so this is not the first frame's cost
    emitter.emit(prev, grid, full=True)
    iterations = 50
    full_payloads = []
    started = time.perf_counter()
    for _ in range(iterations):
        full_payloads.append(emitter.emit(prev, grid, full=True)["bytes"])
    full_ms = (time.perf_counter() - started) / iterations * 1000.0

    prev.copy_from(grid)
    iterations = 200
    diff_payloads = []
    started = time.perf_counter()
    for index in range(iterations):
        grid.put(5, 5, make_cell("X" if index % 2 else "Y"))
        diff_payloads.append(emitter.emit(prev, grid)["bytes"])
        prev.copy_from(grid)
    diff_ms = (time.perf_counter() - started) / iterations * 1000.0

    started = time.perf_counter()
    for _ in range(iterations):
        emitter.emit(prev, grid)
    steady_ms = (time.perf_counter() - started) / iterations * 1000.0

    print("\nrenderer cost on this machine (80x24, fake TTY, in-process, no syscall):")
    print("  full repaint of every cell : %6.3f ms/frame  (%d bytes, %d cells)"
          % (full_ms, max(full_payloads), 80 * 24))
    print("  one-cell diff + retire     : %6.3f ms/frame  (%d bytes)"
          % (diff_ms, max(diff_payloads)))
    print("  frame that changed nothing : %6.3f ms/frame  (0 bytes)" % steady_ms)
    # 12fps leaves 83ms per frame; anything near these bounds is a skin problem, not
    # an engine problem, and a loaded machine should not fail the suite for slack.
    assert full_ms < 25.0 and diff_ms < 10.0 and steady_ms < 5.0
    assert full_ms > diff_ms > steady_ms >= 0.0
    assert max(full_payloads) > 2000 and max(diff_payloads) == 7


def test_strip_terminal_is_still_the_gate_it_claims_to_be():
    assert strip_terminal("\x1b[2Jclean") == "clean"
    assert "\x07" not in strip_terminal("be\u000el")


# ---------------------------------------------------------------------------
# 9. Replay: does the byte stream actually paint the screen we modelled?
# ---------------------------------------------------------------------------

class DumbTerminal:
    """A deliberately independent terminal model: it knows cursor moves and
    double-width glyphs, but none of the renderer's own maths, so a wrong move or a
    stale half-glyph shows up here as a wrong screen rather than a matching counter.
    """

    COVERED = "\x00"  # a column eaten by the left half of a wide glyph

    def __init__(self, cols, rows):
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0
        self.received = 0

    def resize(self, cols, rows):
        """The user dragged the window: a fresh buffer at the new shape.

        Real terminals reflow or discard; what this model must not do is hand the
        engine back columns the new width has no room for.
        """
        self.cols, self.rows = cols, rows
        self.grid = [[" "] * cols for _ in range(rows)]
        self.row = self.col = 0

    def feed(self, text):
        self.received += len(text.encode("utf-8"))
        index, size = 0, len(text)
        while index < size:
            ch = text[index]
            if ch == ESC:
                index += self._escape(text, index)
                continue
            self._put(ch)
            index += 1

    def _escape(self, text, start):
        if start + 1 >= len(text):
            return 1
        if text[start + 1] != "[":
            return 2  # a two-character sequence: nothing a cell model must do
        cursor = start + 2
        while cursor < len(text) and text[cursor] in "0123456789;:?<>!\"'#":
            cursor += 1
        if cursor >= len(text):
            return len(text) - start
        self._csi(text[cursor], text[start + 2:cursor])
        return cursor + 1 - start

    def _csi(self, final, params):
        if params.startswith("?"):
            return  # alt screen / cursor visibility: no cells involved
        numbers = [int(part) if part else 1 for part in params.split(";")] if params else []
        if final in ("H", "f"):
            self.row = (numbers[0] - 1) if len(numbers) > 0 else 0
            self.col = (numbers[1] - 1) if len(numbers) > 1 else 0
        elif final == "C":
            self.col += numbers[0] if numbers else 1
        elif final == "D":
            self.col -= numbers[0] if numbers else 1
        elif final == "J":
            self.grid = [[" "] * self.cols for _ in range(self.rows)]
        # "m" and anything else: colour or noise, and this model checks characters.

    def _put(self, ch):
        if ch in ("", "\r", "\n", "\t"):
            return
        wide = ch in "你好"  # the two glyphs this test draws; a real CJK terminal
        columns = 2 if wide else 1
        if 0 <= self.row < self.rows and 0 <= self.col < self.cols:
            self.grid[self.row][self.col] = ch
            for offset in range(1, columns):
                if self.col + offset < self.cols:
                    self.grid[self.row][self.col + offset] = self.COVERED
        self.col += columns

    def lines(self):
        return ["".join(row).replace(self.COVERED, "") for row in self.grid]


def test_replaying_the_stream_paints_the_screen_the_grid_holds():
    cols, rows = 40, 8
    stream = FakeTTY()
    screen = Screen(stream, size=(cols, rows))
    emitter = Emitter(screen, "truecolor")
    grid = Grid(cols, rows)
    painter = Painter(grid, size=(cols, rows), depth="truecolor")
    terminal = DumbTerminal(cols, rows)

    def drain():
        """Hand the model whatever the engine wrote since the last drain."""
        payload = stream.getvalue()
        stream.truncate(0)
        stream.seek(0)
        terminal.feed(payload)

    painter.draw_box(1, 1, 22, 5, border="honey", title="BeeCode")
    painter.draw_text(3, 3, "привет, мир", color=(255, 204, 0))
    painter.draw_text(3, 4, "你好 spinner|", color="leaf")
    emitter.emit(Grid(cols, rows), grid, full=True)
    drain()
    assert terminal.lines() == [grid.line(y) for y in range(rows)]

    # A second frame: the greeting gains a mark, the wide glyph is erased and the
    # spinner moves on. The model has to follow every cursor move to agree.
    shown = Grid(cols, rows)
    shown.copy_from(grid)
    painter.draw_text(3, 3, "привет, мир!", color=(255, 204, 0))
    painter.clear_region(3, 4, 6, 1)
    painter.draw_text(9, 4, "/", color="leaf")
    second = emitter.emit(shown, grid)
    drain()
    assert terminal.lines() == [grid.line(y) for y in range(rows)], "\n".join(
        "%-42r | %r" % pair for pair in zip(terminal.lines(),
                                            [grid.line(y) for y in range(rows)]))
    assert second["runs"] <= 4 and second["bytes"] < 120, "a diff, not a repaint"
    assert terminal.received < 2200, "the whole screen went twice: %d" % terminal.received


def test_replay_survives_a_resize_without_keeping_yesterdays_cells():
    cols, rows = 30, 5
    stream = FakeTTY()
    screen = Screen(stream, size=(cols, rows))
    emitter = Emitter(screen, "256")
    grid = Grid(cols, rows)
    painter = Painter(grid, size=(cols, rows), depth="256")
    terminal = DumbTerminal(cols, rows)

    painter.draw_text(0, 0, "X" * 30)
    emitter.emit(Grid(cols, rows), grid, full=True)
    payload = stream.getvalue()
    stream.truncate(0)
    stream.seek(0)
    terminal.feed(payload)

    # Same engine, narrower screen: the user dragged the window (the model takes the
    # new shape), the engine clears and repaints, and the columns that fell off the
    # right edge must not stay on the screen.
    terminal.resize(12, rows)
    grid.resize(12, rows)
    Painter(grid, size=(12, rows), depth="256").draw_text(0, 0, "Y" * 12)
    result = emitter.emit(Grid(12, rows), grid, full=True)
    terminal.feed(stream.getvalue())
    assert result["cleared"] and result["full"]
    assert terminal.lines() == [grid.line(y) for y in range(rows)]
    assert terminal.lines()[0] == "Y" * 12


# ---------------------------------------------------------------------------
# 10. Sizes: the default that keeps a frame whole
# ---------------------------------------------------------------------------

def test_nonsense_sizes_clamp_and_the_default_survives_a_stream_that_cannot_answer(monkeypatch):
    assert clamp_size(0, 0) == (1, 1)
    assert clamp_size(10 ** 6, -5) == (R.MAX_DIM, 1)
    assert clamp_size("eighty", "24") == R.DEFAULT_SIZE
    monkeypatch.delenv("COLUMNS", raising=False)
    monkeypatch.delenv("LINES", raising=False)

    def nothing_to_ask(fd=None):
        raise OSError("no terminal behind this fd")

    monkeypatch.setattr(R.os, "get_terminal_size", nothing_to_ask)
    assert terminal_size(FakeTTY()) == R.DEFAULT_SIZE, "80x24, and not a crash"
    assert terminal_size(io.StringIO()) == R.DEFAULT_SIZE

    class Grumpy(io.StringIO):
        def isatty(self):
            raise RuntimeError("state says no")

    assert terminal_size(Grumpy()) == R.DEFAULT_SIZE


def test_the_grid_never_becomes_a_monster_because_the_environment_said_so():
    assert clamp_size(999999, 999999) == (R.MAX_DIM, R.MAX_DIM)
    loop = FrameLoop(lambda p: p.draw_text(0, 0, "hi"), stream=FakeTTY(), size=(2000, 5))
    assert (loop.grid.cols, loop.grid.rows) == (R.MAX_DIM, 5)
    assert loop.run(fps=30, max_frames=1)["frames"] == 1


# ------------------------------------------------------- widgets and tweens --
#
# A skin that owns a line of the interface has to build it every frame. Everything
# here exists so that "make the status line shimmer" is one call: without these
# helpers every skin writes the same hex arithmetic, and the ones that get it wrong
# show colours the terminal cannot draw.

def test_blend_endpoints_and_clamping():
    assert R.blend("#000000", "#ffffff", 0.0) == "#000000"
    assert R.blend("#000000", "#ffffff", 1.0) == "#ffffff"
    assert R.blend("#ffcc00", "#7cb342", 2.0) == "#7cb342", "past the end holds"
    assert R.blend("#ffcc00", "#7cb342", -1.0) == "#ffcc00"
    assert R.blend("#ffcc00", "#7cb342", 0.5) == "#bec021"


def test_named_curves_go_the_way_their_names_say():
    assert R.ease("linear", 0.4) == pytest.approx(0.4)
    assert R.ease("in", 0.5) < 0.5 and R.ease("out", 0.5) > 0.5
    assert R.ease("in_out", 0.0) == 0.0 and R.ease("in_out", 1.0) == pytest.approx(1.0)
    assert R.ease("pulse", 0.5) == pytest.approx(1.0)
    assert R.ease("not-a-curve", 0.3) == pytest.approx(0.3), "an unknown name is linear"
    assert R.ease("linear", "junk") == 0.0 and R.ease("linear", None) == 0.0


def test_phase_wraps_instead_of_running_off_the_screen():
    assert R.phase(0.0, 2.0) == 0.0
    assert R.phase(1.0, 2.0) == pytest.approx(0.5)
    assert R.phase(7.5, 2.0) == pytest.approx(0.75), "the cycle continues, not climbs"
    assert R.phase(None) == 0.0


def test_markup_escapes_a_bracket_because_the_text_is_the_model_s_bytes():
    assert R.markup("a", fg="bold red") == "[bold red]a[/]"
    assert R.markup("") == ""
    assert R.markup("list[0]") == r"list\[0]", "no markup is read out of the payload"
    assert R.markup("[bold] hi") == r"\[bold] hi"
    assert R.markup("x") == "x", "no colour asked, nothing added"
    assert R.markup("x", "red", "#0000ff", "bold") == "[red #0000ff bold]x[/]"


def test_ramp_markup_colours_every_character_once():
    line = R.ramp_markup("abc", "#ffcc00", "#7cb342")
    assert line.count("[/") == 3 and line.count("[#") == 3
    assert "#7cb342" in line and "#ffcc00" in line, "the ends are the colours asked for"
    assert "b" in line and R.ramp_markup("", "#fff", "#000") == ""


def test_the_text_widgets_answer_with_what_they_were_given():
    assert R.bar_markup(0.5, 8).startswith("████░░░░")
    assert R.bar_markup(1.0, 4, label="100%") == "████ 100%"
    assert R.bar_markup(0.0, 4) == "░░░░"
    assert R.bar_markup("junk", 4) == "", "a number that is not one draws nothing"
    assert len(R.spark_markup([0, 5, 10])) == 3
    assert R.spark_markup([]) == "" and R.spark_markup(["x"]) == ""
    assert R.spark_markup(list(range(50)), width=6) != ""
    assert R.ticker_markup("short", 40) == "short"
    scrolling = R.ticker_markup("длинная строка состояния", 8, 0.25)
    assert len(scrolling.replace("[", "").replace("]", "")) >= 8


def test_blink_keeps_the_width_so_the_line_does_not_jump():
    assert R.blink(True, "●") == "●"
    assert R.blink(False, "●") == " ", "space, not nothing: the rest of the line holds"
    assert R.blink(False, "ab") == "  " and R.blink(False, "ab", off="-") == "-"


def test_the_painter_widgets_write_cells_and_report_the_clipped_ones():
    painter = R.Painter(size=(20, 3), depth="truecolor")
    assert painter.draw_bar(0, 0, 10, 0.5, color="#ffcc00") == 10
    assert painter.grid.line(0).startswith("█████░░░░░")
    assert painter.draw_bar(15, 1, 10, 1.0, color="#ffcc00") == 5, "clipped, and counted"
    assert painter.draw_ramp(0, 2, 6, "#ffcc00", "#7cb342") == 6
    assert painter.draw_sparkline(0, 0, 5, [1, 2, 3, 4, 5]) == 5
    assert painter.draw_ticker(0, 1, 12, "hello world and more", 0.0) == 12


def test_a_widget_given_nonsense_warns_and_draws_nothing():
    """A skin can hand the painter anything; the frame still has to finish."""
    painter = R.Painter(size=(20, 3), depth="truecolor")
    assert painter.draw_bar(0, 0, "wide", None) == 0
    assert painter.draw_sparkline(0, 0, 4, ["a"]) == 0
    assert painter.draw_ramp(0, 0, 4, None, None, fraction="z") == 0
    assert painter.draw_ticker(0, 0, 5, "") == 0
    joined = " | ".join(painter.frame_warnings)
    assert "bar" in joined and "sparkline" in joined, painter.frame_warnings


def test_a_skin_can_read_the_cell_it_is_sitting_on():
    painter = R.Painter(size=(12, 2), depth="truecolor")
    painter.draw_text(2, 0, "х", color="#ffcc00")
    assert painter.cell(2, 0).ch == "х"
    assert painter.cell(0, 0).ch == " "
    assert painter.cell(99, 99) is None and painter.cell("a", 0) is None
