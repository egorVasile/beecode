"""The surface contract: a skin asks for a piece of the UI and the host keeps it to it.

Every case here is about who is allowed to say what the interface looks like:

* an unclaimed surface is BeeCode's, and asking about it costs the skin nothing;
* a claim is refused with a reason a person can act on — a typo, or a hook the
  skin never wrote;
* a skin that breaks on one surface loses *that* surface and keeps the rest, so a
  bad `on_stream` does not take the status line down with it;
* and losing it is *announced*: a UI that quietly stops animating reads as a UI
  that was never animating.

`None` from a text hook means "keep your wording"; an empty string means "show
nothing here". That difference is what most of these tests are for.

The skins below are installed from source text, so the AST gate runs on them like
on anything from disk — which is why none of them imports more than `time`.
"""
import time
import types

import pytest

from beeagent import i18n
from beeagent.core import skins


@pytest.fixture(autouse=True)
def isolated():
    """A clean registry, notices collected instead of printed, English wording."""
    previous = i18n.get_lang()
    was_budget = skins.budget()
    i18n.set_lang("en")
    skins.reset()
    seen = []
    skins.set_notifier(seen.append)
    yield seen
    skins.set_notifier(None)
    skins.reset()
    skins.configure(**was_budget)
    i18n.set_lang(previous)


def use(source: str, name: str = "probe") -> str:
    """Install a skin from source text and put it on screen."""
    skins.register(name, source=source, description="test")
    refusal = skins.switch(name)
    assert not refusal, refusal
    return name


# ------------------------------------------------------------- claiming -------

def test_a_skin_that_claims_a_surface_is_asked_about_it(isolated):
    use("""
SURFACES = ("status",)


def on_status(default):
    return "S:" + str(default)
""")
    assert skins.owns("status") is True
    assert skins.owns("spinner") is False
    assert skins.status_text("thinking") == "S:thinking"


def test_an_unclaimed_surface_answers_with_the_built_in_text(isolated):
    use("""
def on_status(default):
    return "nope"
""")
    assert skins.owns("status") is False
    assert skins.status_text("the real line") == "the real line"


def test_a_claim_for_a_surface_that_does_not_exist_is_refused_by_name(isolated):
    use("""
SURFACES = ("sidbar",)


def on_sidbar(default):
    return ""
""")
    report = skins.surfaces()
    assert "sidbar" in report["refused"], report
    assert "status" in report["refused"]["sidbar"], "name the surfaces there are"
    assert skins.owns("sidbar") is False


def test_claiming_without_writing_the_hook_is_refused(isolated):
    use("""
SURFACES = ("status", "spinner")


def on_status(default):
    return "x"
""")
    report = skins.surfaces()
    assert report["held"] == ["status"], report
    assert "spinner" in report["refused"]
    assert "on_spinner" in report["refused"]["spinner"]


def test_on_surfaces_may_decide_from_the_context_it_is_handed(isolated):
    use("""
def on_surfaces(ctx):
    return ["status"] if ctx.size()[0] > 20 else []


def on_status(default):
    return "wide"
""")
    assert skins.owns("status") is True
    assert skins.status_text("d") == "wide"


def test_a_hook_that_raises_in_on_surfaces_claims_nothing(isolated):
    use("""
def on_surfaces(ctx):
    raise RuntimeError("no")


def on_status(default):
    return "x"
""")
    assert skins.surfaces()["held"] == []
    assert "*" in skins.surfaces()["refused"]


# ------------------------------------------------------------- answering ------

def test_none_keeps_beecode_s_wording_and_empty_string_is_a_choice(isolated):
    use("""
SURFACES = ("status", "spinner")


def on_status(default):
    return None


def on_spinner(default, dt):
    return ""
""")
    assert skins.status_text("built-in") == "built-in"
    assert skins.spinner_text("built-in", 0.1) == "", "an empty line is what it asked for"


def test_a_number_from_a_text_hook_loses_the_surface_not_the_screen(isolated):
    use("""
SURFACES = ("status", "spinner")


def on_status(default):
    return 42


def on_spinner(default, dt):
    return "tick"
""")
    assert skins.status_text("mine") == "mine"
    assert skins.owns("status") is False
    assert skins.spinner_text("mine", 0.0) == "tick", "the other surface still works"
    assert any("status" in line for line in isolated), isolated


def test_a_raising_hook_costs_only_its_own_surface(isolated):
    use("""
SURFACES = ("status", "thinking")


def on_status(default):
    raise ValueError("boom")


def on_thinking(default):
    return "kept"
""")
    assert skins.status_text("mine") == "mine"
    assert skins.thinking_text("mine") == "kept"
    assert "status" in skins.surfaces()["dropped"]
    assert "ValueError" in skins.surfaces()["dropped"]["status"]


def _late(moves: int = 400000) -> str:
    """A skin whose status hook works too hard — measured in steps, not seconds.

    The budget is moved to 1 ms by the test that uses this, so what counts as late
    is decided by the numbers here and not by how loaded the machine happens to be.
    """
    return ("""
SURFACES = ("status",)


def on_status(default):
    total = 0
    for i in range({moves}):
        total += i % 7
    return "spent"
""").format(moves=moves)


def test_a_surface_late_three_times_is_taken_away_and_said(isolated):
    skins.configure(frame_budget_ms=1.0, event_budget_ms=1.0, hard_cap_factor=500.0)
    use(_late())
    for _ in range(3):
        assert skins.status_text("mine") == "spent"
    assert skins.owns("status") is True, "three late calls are the grace, not four"
    assert skins.status_text("mine") == "mine"
    assert skins.owns("status") is False
    # The grace is three late calls; the fourth is the one that costs the surface,
    # and the reason says how many it took.
    assert "4 times" in skins.surfaces()["dropped"]["status"]


def test_a_skin_that_recovers_between_frames_keeps_its_surface(isolated):
    """Overruns have to be *consecutive*: this box compiles while a skin draws.

    Measured here on 2026-09-26: an `on_hud` that does nothing at all spiked past
    8 ms on the same machine that runs the agent, so a counter that never reset
    would take the status line away from a working skin an hour into a session —
    a failure nobody could reproduce, and no notice that made sense of. A skin
    that is really late has no good frames to reset on, so it still loses it.
    """
    skins.configure(frame_budget_ms=1.0, event_budget_ms=1.0, hard_cap_factor=500.0)
    use("""
SURFACES = ("status",)
CALLS = 0


def on_status(default):
    global CALLS
    CALLS += 1
    if CALLS % 2:
        total = 0
        for i in range(400000):
            total += i % 7
    return "spent"
""")
    for _ in range(12):
        assert skins.status_text("mine") == "spent", "lost between late frames"
    assert skins.owns("status") is True
    assert skins.surfaces()["dropped"] == {}, skins.surfaces()


def test_a_single_very_late_call_stops_the_skin_entirely(isolated):
    """Once per token far past the ceiling is a hang, and a hang is not a style choice."""
    skins.configure(frame_budget_ms=1.0, event_budget_ms=1.0, hard_cap_factor=2.0)
    use(_late())
    assert skins.status_text("mine") == "mine"
    report = skins.surfaces("probe")
    assert "status" in report["dropped"]
    assert skins.owns("status") is False


def test_switching_again_gives_the_surfaces_back(isolated):
    use("""
SURFACES = ("status",)


def on_status(default):
    raise RuntimeError("once")
""")
    skins.status_text("mine")
    assert skins.owns("status") is False
    skins.switch("probe")
    assert skins.owns("status") is True, "a fresh switch is a fresh chance"
    assert skins.surfaces()["dropped"] == {}


# ------------------------------------------------------------------ the HUD ---

def test_hud_rows_are_clamped_and_the_cut_is_said(isolated):
    use("""
SURFACES = ("hud",)
HUD_ROWS = 9


def on_hud(painter, dt):
    return None
""")
    assert skins.surfaces()["hud_rows"] == skins.HUD_MAX_ROWS
    assert any("hud asked for 9" in line for line in isolated), isolated


def test_hud_is_not_painted_for_a_skin_that_does_not_own_it(isolated):
    use("""
SURFACES = ("status",)


def on_status(default):
    return "x"
""")
    assert skins.hud_frame(object(), 0.05) is False


def test_hud_frame_calls_the_hook_and_drops_it_on_an_exception(isolated):
    use("""
SURFACES = ("hud",)


def on_hud(painter, dt):
    painter.calls.append(dt)
""")
    grid = types.SimpleNamespace(calls=[])
    assert skins.hud_frame(grid, 0.05) is True
    assert grid.calls == [0.05]
    use("""
SURFACES = ("hud",)


def on_hud(painter, dt):
    raise KeyError("k")
""", name="broken")
    assert skins.hud_frame(grid, 0.05) is False
    assert "hud" in skins.surfaces("broken")["dropped"]


# ------------------------------------------------------- reporting and reset --

def test_a_report_lists_what_the_skin_on_screen_changed(isolated):
    use("""
SURFACES = ("status", "stream")


def on_status(default):
    return "a"


def on_stream(default, done):
    return str(default).upper()
""")
    report = skins.surfaces()
    assert report["held"] == ["status", "stream"]
    assert report["refused"] == {}
    assert skins.stream_text("hi", False) == "HI"


def test_a_refused_skin_keeps_its_own_refusal_instead_of_a_claim(isolated):
    """A source the gate stops has no surfaces to talk about."""
    skins.register("bad", source="import os\n", description="test")
    assert skins.switch("bad") != ""
    assert skins.surfaces("bad")["held"] == []


def test_the_new_hooks_are_reported_as_hooks_of_the_skin(isolated):
    use("""
SURFACES = ("status",)


def on_status(default):
    return "x"
""")
    assert "on_surfaces" in skins.HOOKS and "on_hud" in skins.HOOKS
    assert "on_status" in skins.hooks_of(types.SimpleNamespace(
        on_status=lambda default: "", on_surfaces=lambda ctx: ()))


def test_the_context_carries_the_language_the_user_chose(isolated):
    """A skin may not import `beeagent.i18n`, so the host hands the words over.

    Without this the reference packs' `say(en, ru)` reads a language that is never
    set, and a Russian user gets an English status line from a skin that wrote both.
    """
    use("""
WORDS = ("", "")


def on_init(ctx):
    global WORDS
    WORDS = (ctx.language(), ctx.translate("thinking", "думаю"))


SURFACES = ("status",)


def on_status(default):
    return "%s:%s" % WORDS
""")
    assert skins.status_text("x") == "en:thinking", skins.status_text("x")
    i18n.set_lang("ru")
    use("""
def on_init(ctx):
    global LANG, WORD
    LANG, WORD = ctx.language(), ctx.translate("thinking", "думаю")


SURFACES = ("status",)


def on_status(default):
    return LANG + ":" + WORD
""", name="russian")
    assert skins.status_text("x") == "ru:думаю", skins.status_text("x")
