"""`skin-prism` — the pack that owns seven surfaces at once.

What these tests hold shut is the promise the user asked for: a logo that cycles,
borders that shimmer, an answer that is still the model's answer. The first is a
contract check (the pack registers, claims and is worn by its shelf name), the rest
are cost and fidelity checks, because a skin that paints the whole screen is also a
skin that can spend the whole frame budget painting it.

The numbers in `PRISM_BUDGET` are not decoration: they are the medians measured on
this box, with room for a machine doing something else at the same time. A pack that
gets slower trips them; a box that gets slower trips nothing.

Nothing here reaches the network or the developer's working tree.
"""
import importlib.util
import io
import math
import statistics
import sys
import time
from pathlib import Path

import pytest

from beeagent.core import skins

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "beeagent" / "plugins" / "templates" / "plugins" / "skin-prism" / "plugin.py"

#: Median cost of one call, in milliseconds, measured on this box 2026-09-26.
#: `banner` is the five-row logo the classic interface prints at startup — thirty-
#: odd calls, once. `banner_row` is the one row the full-screen header repaints
#: *twelve times a second*, so its number matters more than it looks: 2 ms there is
#: 24 ms a second, about two and a half percent of one core spent on a word in the
#: corner. Both figures are medians with room for a machine doing something else.
PRISM_BUDGET = {
    "banner": 6.0,
    "banner_row": 3.0,
    "answer": 6.0,
    "frame_color": 0.2,
    "line_hooks": 0.5,
}

ANSWER = """# Result

Prose with `inline code`, **strong words**, a [bracket] that has to survive, and
enough ordinary words in this line that it wraps on any terminal under 90 columns.

- first bullet with a note
- second bullet
1. numbered one
2. numbered two

> a quoted line

```python
def prism(x):
    return {"a": 1, "b": [2, 3], "c": "text with [brackets] too"}
```
"""


@pytest.fixture(scope="module")
def prism_source():
    return PACK.read_text(encoding="utf-8")


@pytest.fixture
def clean_skins(monkeypatch):
    """An empty registry and a cold clock, so this file is not reading a neighbour.

    `test_skins_host.py` has the same fixture for its own file; a pack test that
    borrowed it would pass because of the order the two files ran in.
    """
    captured = []
    monkeypatch.setattr(skins, "_REGISTRY", {})
    monkeypatch.setattr(skins, "_NOTICES", __import__("collections").deque(maxlen=50))
    monkeypatch.setattr(skins, "_UNKNOWN", {})
    monkeypatch.setattr(skins, "_LOOP", {})
    monkeypatch.setattr(skins, "_ACTIVE", "")
    monkeypatch.setattr(skins, "_NOTIFIER", captured.append)
    monkeypatch.setattr(skins, "_BUDGET", dict(skins.budget()))
    return captured


@pytest.fixture
def prism(clean_skins, prism_source):
    """The pack loaded the way the plugin loader loads it, worn, and its module."""
    from beeagent.ext.api import ExtensionRegistry

    spec = importlib.util.spec_from_file_location("prism_under_test", str(PACK))
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    class Api:
        plugin = "skin-prism"

        def __init__(self):
            self.registry = ExtensionRegistry()

        def skin_hooks(self, name, hooks, description=""):
            return not skins.register(name, hooks, description=description,
                                      pack=self.plugin).refused

    module.setup(Api())
    assert skins.switch("prism") == "", "the pack registered a skin that cannot be worn"
    module.worn = skins
    yield module
    sys.modules.pop(spec.name, None)


def _median(label, fn, n=120):
    from beeagent.core.renderer import Painter  # noqa: F401  (callers pass one)

    times = []
    for i in range(n):
        started = time.perf_counter()
        fn(i)
        times.append((time.perf_counter() - started) * 1000.0)
    times.sort()
    return statistics.median(times), times[-1]


# ---------------------------------------------------------------- the contract --

def test_the_pack_passes_the_gate_and_owns_seven_lines(prism_source):
    """A stranger's file that reaches this folder is still read by the gate."""
    import ast

    assert skins.check_source(prism_source) == [], "the shipped pack is refused by the gate"
    reached = set()
    for node in ast.walk(ast.parse(prism_source)):
        if isinstance(node, ast.Import):
            reached |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            reached.add(node.module)
    allowed = skins.IMPORT_ALLOWLIST
    outside = {name for name in reached if name.split(".")[0] not in
               {a.split(".")[0] for a in allowed}}
    assert not outside, f"the pack imports {sorted(outside)}, which the gate does not allow"
    assert not any(name.startswith("beeagent.ui") for name in reached), \
        "a pack that reaches the interface modules bypasses the surfaces"


def test_the_shelf_name_wears_it(prism):
    assert skins.resolve("skin-prism") == "prism"
    assert skins.for_pack("skin-prism") == ["prism"]
    report = skins.surfaces("prism")
    assert sorted(report["held"]) == ["answer", "banner", "frame", "hud", "spinner",
                                      "status", "thinking"], report
    assert report["refused"] == {}, report
    assert 0 < report["hud_rows"] <= skins.HUD_MAX_ROWS, report


def test_it_does_not_claim_the_streaming_line(prism):
    """`stream` is the model's bytes on their way to the log; the pack paints the
    block, not the wire."""
    assert not skins.owns("stream"), "the pack rewrote the streaming surface"


# ------------------------------------------------------------------- the logo ---

def test_the_logo_loops_without_stopping(prism):
    """A cycle, not a blink: many distinct *colourings*, and the same one again a
    full period later. Two clocks run at once here — a crest across the letters and
    a slower rotation of the palette — so the period is their meeting point.

    The colours are what moves, so the comparison reads the Text's spans: `str()`
    of a Rich Text drops every style, and two frames that differ only in colour
    would look identical to a test written carelessly. (That test was written
    carelessly once already.)
    """
    from beeagent.ui.components import banner_static

    shipped = banner_static()

    def frame(at):
        painted = skins.banner_render(seconds=at, size=(70, 5), default=shipped)
        assert painted is not None, "the skin gave up the logo mid-cycle"
        return (str(painted), tuple((span.start, span.end, str(span.style))
                                    for span in painted.spans))

    seen = {frame(step * 0.13) for step in range(24)}
    assert len(seen) >= 8, f"the logo has {len(seen)} distinct frames in 24, not a cycle"
    # The two clocks share a beat only where *both* have done a whole number of
    # turns: 3.2 s and 11 s meet at 176 s, not at their product (35.2 is eleven
    # crests but only three and a fifth rotations). The long seam is the point of
    # choosing periods that do not divide each other.
    from fractions import Fraction

    def lcm(first: Fraction, second: Fraction) -> Fraction:
        """LCM of two rationals: LCM of the numerators over GCD of the denominators."""
        return Fraction(first.numerator * second.numerator //
                        math.gcd(first.numerator, second.numerator),
                        math.gcd(first.denominator, second.denominator))

    period = float(lcm(Fraction(str(prism.PRISM_WAVE_SECONDS)),
                       Fraction(str(prism.PRISM_DRIFT_SECONDS))))
    assert period == 176.0, f"3.2 s and 11 s should meet at 176 s, not {period}"
    assert frame(0.0) == frame(period), "the two clocks do not meet up again"
    assert frame(0.0) != frame(period / 2), "the cycle is half-empty"


def test_the_logo_never_changes_its_letters(prism):
    """Colour is the animation; the word is not. A logo whose ink moves reads as a
    broken glyph set, and at 30 columns it reads as garbage."""
    from beeagent.ui.components import banner_static

    def ink(at):
        painted = skins.banner_render(seconds=at, size=(70, 5), default=banner_static())
        return [tuple(index for index, ch in enumerate(line) if ch not in " \t")
                for line in str(painted).split("\n")]

    first = ink(0.0)
    for step in range(1, 20):
        assert ink(step * 0.27) == first, f"the logo's shape moved at frame {step}"


def test_the_logo_survives_a_narrow_terminal(prism):
    from beeagent.ui.components import banner_static

    for width in (30, 40, 60, 120):
        painted = skins.banner_render(seconds=1.7, size=(width, 5), default=banner_static())
        assert painted is not None
        lines = [line for line in str(painted).split("\n") if line]
        assert lines, f"a {width}-column logo drew nothing"
        for line in lines:
            assert len(line) <= width + 2, f"{width} columns: {line!r}"


# ------------------------------------------------------------------- the frames --

def test_each_panel_role_sits_elsewhere_on_the_same_cycle(prism):
    """Six panels on one screen have to read as a prism, not as one light bulb."""
    roles = ("answer", "tool", "output", "prompt", "picker")
    colours = {role: skins.frame_color(role, "bold green") for role in roles}
    assert all(c and c != "bold green" for c in colours.values()), colours
    assert len({c.split()[-1] for c in colours.values()}) >= 3, colours


def test_an_error_panel_stays_red(prism):
    """An animation that hides a failure is worse than no animation."""
    from beeagent.core import renderer

    for step in range(30):
        style = skins.frame_color("error", "bold red")
        rgb = renderer.token_rgb(style.split()[-1])
        assert rgb and max(rgb[:2]) > 100 and rgb[2] < max(rgb[:2]), \
            f"step {step}: {style} is not a red-family colour"


def test_a_role_the_pack_does_not_know_keeps_bee_codes_colour(prism):
    """Asking about an unknown panel must not paint it nothing."""
    kept = skins.frame_color("nonsense", "bold green")
    assert kept, "an unknown role came back empty, which erases the border"


# -------------------------------------------------------------------- the answer --

def test_every_character_the_model_wrote_is_still_there(prism):
    """The one promise an answer skin may not break: restyle, never rewrite."""
    from rich.console import Console

    painted = skins.answer_render(ANSWER, True)
    assert painted is not None, "the pack holds `answer` but drew nothing"
    buffer = io.StringIO()
    Console(file=buffer, width=88, force_terminal=False, color_system=None).print(painted)
    out = buffer.getvalue()
    missing = [line for line in ANSWER.split("\n")
               if line.strip() and line.strip() not in out]
    assert not missing, "the answer lost lines: " + repr(missing[:3])
    assert "[brackets]" in out, "a bracket in code has to come out as typed"


def test_the_answer_is_the_same_when_the_terminal_has_no_colour(prism, monkeypatch):
    """Colour is the decoration; the text is the product."""
    from rich.console import Console

    from beeagent.core import renderer

    monkeypatch.setattr(renderer, "color_support", lambda refresh=False: "none")
    buffer = io.StringIO()
    Console(file=buffer, width=88, force_terminal=False, color_system=None).print(
        skins.answer_render(ANSWER, True))
    out = buffer.getvalue()
    assert "# Result" in out and "def prism(x):" in out, out[:200]
    assert "[bracket]" in out, "the model's own brackets have to come out as typed"
    assert "[/]" not in out, "a closing tag leaked into the text: the markup was " \
                             "written by the pack, not by the model"


# ----------------------------------------------------------------------- cost ---

def test_the_pack_stays_inside_its_own_measured_budget(prism):
    """Median cost per call, with the worst frame reported and not judged.

    The hard cap belongs to the host; what this test protects is the steady state:
    a skin that answers the logo in 40 ms once is a stall, but a skin that answers
    it in 40 ms every frame is a different bug and has to be named.
    """
    from beeagent.core.renderer import Painter
    from beeagent.ui.components import banner_static, BANNER_ROWS, BANNER_WIDTH

    painter = Painter(size=(78, 2))
    cases = {
        "banner": lambda i: skins.banner_render(seconds=i * 0.08,
                                                size=(BANNER_WIDTH, len(BANNER_ROWS)),
                                                default=banner_static()),
        "banner_row": lambda i: skins.banner_render(seconds=i * 0.08,
                                                    size=(BANNER_WIDTH, 1),
                                                    default=banner_static()),
        "answer": lambda i: skins.answer_render(ANSWER * 6, i % 2 == 0),
        "frame_color": lambda i: skins.frame_color("answer", "bold green"),
        "line_hooks": lambda i: (skins.status_text("idle"),
                                 skins.spinner_text("working", i * 0.08),
                                 skins.thinking_text("a line")),
    }
    for name, fn in cases.items():
        median, worst = _median(name, fn)
        assert median <= PRISM_BUDGET[name], (
            f"{name} costs {median:.2f} ms per call, the measured budget is "
            f"{PRISM_BUDGET[name]:.2f} ms (worst frame this run: {worst:.1f} ms)")
    assert skins.stats("prism")["demoted"] is False
    assert skins.surfaces("prism")["dropped"] == {}, skins.surfaces("prism")


def test_the_hud_fits_the_rows_it_asked_for(prism):
    from beeagent.core.renderer import Painter

    rows = skins.surfaces("prism")["hud_rows"]
    painter = Painter(size=(60, rows))
    for step in range(30):
        assert skins.hud_frame(painter, 0.08) is True
        for row in range(rows):
            line = painter.grid.line(row)
            assert len(line) <= 60, repr(line)
            assert line.strip(), f"row {row} of the HUD drew nothing"
