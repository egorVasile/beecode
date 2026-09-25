"""The claimed surfaces asked from the interface itself, not from the host's unit.

A contract nobody calls is a document. These cases drive the real Textual app and
the real streaming renderer: install a skin that claims surfaces, then read what a
person would see — the status label, the HUD label, the waiting line, the printed
answer — and require the skin's words to be there.

The failure each one prevents is the same shape: the host keeps swallowing an
error in a skin path (`except Exception: pass`, because a broken decoration must
not eat an answer), and without a test on the visible surface that swallow hides a
typo, a missing import, or a whole feature that never ran.
"""
import asyncio
import io

import pytest
from rich.console import Console

from beeagent import i18n
from beeagent.core import skins
from beeagent.core.session import Session
from beeagent.config.schema import BeeConfig


@pytest.fixture()
def english():
    previous = i18n.get_lang()
    i18n.set_lang("en")
    skins.reset()
    skins.set_notifier(lambda text: None)
    yield
    skins.set_notifier(None)
    skins.reset()
    i18n.set_lang(previous)


def use(source: str, name: str = "probe") -> str:
    skins.register(name, source=source, description="test")
    refusal = skins.switch(name)
    assert not refusal, refusal
    return name


def add(source: str, name: str = "probe") -> str:
    """Register without switching: the state before a person types `/skins <name>`."""
    skins.register(name, source=source, description="test")
    return name


def app_running(tmp_path):
    from beeagent.ui.tui import BeeCodeApp

    app = BeeCodeApp(config=BeeConfig(), session=Session())
    app.agent.workdir = str(tmp_path)
    return app


# ------------------------------------------------------------- the status line --

def test_the_status_label_shows_what_the_skin_says(tmp_path, english):
    """The TUI's state line is the skin's if it claimed it, and BeeCode's if not."""
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            use("""
SURFACES = ("status",)


def on_status(default):
    return "HUD: " + str(default)
""")
            app._update_status()
            await pilot.pause(0.2)
            status = app.home.query_one("#status")
            plain = str(status.render())
            assert plain.startswith("HUD: model "), plain
            skins.switch("off")
            app._update_status()
            await pilot.pause(0.2)
            back = str(app.home.query_one("#status").render())
            assert back.startswith("model "), back
    asyncio.run(scenario())


def test_the_frame_clock_only_runs_when_the_skin_needs_it(tmp_path, english):
    """A 12 fps timer for a skin that draws nothing is a battery nobody agreed to."""
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            use("""
SURFACES = ("status",)


def on_status(default):
    return "plain"
""")                                  # no on_frame, no hud: no clock wanted
            app._sync_skin_clock()
            assert app._skin_timer is None

            use("""
def on_frame(painter, dt):
    return None
""", name="animating")
            app._sync_skin_clock()
            assert app._skin_timer is not None
            skins.switch("off")
            app._sync_skin_clock()
            assert app._skin_timer is None
    asyncio.run(scenario())


def test_a_claimed_hud_paints_rows_into_the_hud_label(tmp_path, english):
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            label = app.home.query_one("#hud")
            assert label.display is False, "no skin, no strip"
            use("""
SURFACES = ("hud",)
HUD_ROWS = 2


def on_hud(painter, dt):
    painter.draw_text(0, 0, "HIVE", color="#ffcc00")
    painter.draw_bar(0, 1, 6, 0.5, color="#ffcc00")
""")
            app._paint_hud()
            await pilot.pause(0.2)
            assert label.display is True
            plain = str(label.render())
            assert "HIVE" in plain and "███░░░" in plain, repr(plain)

            skins.switch("off")
            app._paint_hud()
            await pilot.pause(0.1)
            assert label.display is False, "the strip goes away with the skin"
    asyncio.run(scenario())


def test_a_hud_that_raises_is_lost_and_the_label_empties(tmp_path, english):
    """The strip disappears instead of freezing the frame it was painted in."""
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            use("""
SURFACES = ("hud",)


def on_hud(painter, dt):
    raise IndexError("nope")
""")
            app._paint_hud()
            await pilot.pause(0.2)
            assert skins.owns("hud") is False
            assert app.home.query_one("#hud").display is False
    asyncio.run(scenario())


# ------------------------------------------------ the clock that moves the lines --

COUNTER = """
TICK = 0


def on_frame(dt, painter):
    global TICK
    TICK += 1


SURFACES = ("spinner", "status")


def on_spinner(default, clock):
    return "tick %d" % TICK


def on_status(default):
    return "frame %d" % TICK
"""


async def type_command(pilot, app, line: str) -> None:
    """Put a command into the prompt and press Enter, the way a person does."""
    app.prompt.focus()
    app.prompt.value = line
    await pilot.pause(0.05)
    await pilot.press("enter")
    await pilot.pause(0.3)


def frame_no(text: str) -> int:
    """The number the counting skin wrote into its line, or -1 when it wrote none."""
    for word in reversed(str(text).split()):
        digits = "".join(ch for ch in word if ch.isdigit())
        if digits:
            return int(digits)
    return -1


def test_the_waiting_line_moves_while_the_model_is_silent(tmp_path, english):
    """A spinner a skin owns has to animate in the interface, not in a unit test.

    The waiting line is drawn once by the `status` event; without the frame clock
    offering it again, a claim on `spinner` buys a static line and the author finds
    out by staring at a terminal that never moves.
    """
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            add(COUNTER, name="counting")
            await type_command(pilot, app, "/skins counting")
            app._on_agent_event("status", {})
            assert frame_no(str(app.stream.render())) >= 0, "not the skin's line"
            first = frame_no(str(app.stream.render()))
            await pilot.pause(0.4)                     # the app's own clock, not ours
            later = frame_no(str(app.stream.render()))
            assert later > first, f"the waiting line froze at tick {first}"
            skins.switch("off")
    asyncio.run(scenario())


def test_the_status_label_moves_with_the_skin_too(tmp_path, english):
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            add(COUNTER, name="counting")
            await type_command(pilot, app, "/skins counting")
            first = frame_no(str(app.home.query_one("#status").render()))
            app._skin_tick()
            await pilot.pause(0.2)
            later = frame_no(str(app.home.query_one("#status").render()))
            assert later > first, f"the status line froze at frame {first}"
            skins.switch("off")
    asyncio.run(scenario())


def test_the_clock_never_writes_over_the_answer_or_a_thought(tmp_path, english):
    """The waiting line owns that space only until the model says anything.

    What this prevents is the animation eating the answer: the same code path that
    redraws the joke would redraw it over the first sentence the model sent.
    """
    app = app_running(tmp_path)

    async def scenario():
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause(0.3)
            add(COUNTER, name="counting")
            await type_command(pilot, app, "/skins counting")
            app._on_agent_event("status", {})
            app._on_agent_event("stream_delta", {"text": "the answer"})
            await pilot.pause(0.4)
            assert str(app.stream.render()) == "the answer", str(app.stream.render())
            app._on_agent_event("reasoning_delta", {"text": "a thought"})
            await pilot.pause(0.4)
            assert "a thought" in str(app.stream.render()), str(app.stream.render())
            skins.switch("off")
    asyncio.run(scenario())


# ------------------------------------------------------- the streaming surfaces --

def test_the_waiting_line_is_the_skin_s_phrase(tmp_path, english):
    from beeagent.ui.components import pending_text

    assert "pelmeni" in pending_text() or pending_text(), "the built-in line first"
    use("""
SURFACES = ("spinner",)


def on_spinner(default, clock):
    return "counting cells"
""")
    assert pending_text() == "counting cells"


def test_an_unclaimed_spinner_line_keeps_the_builtin_wording(tmp_path, english):
    from beeagent.ui.components import PENDING_STATES, pending_text

    use("""
def on_spinner(default, clock):
    return "never asked"
""")
    line = pending_text()
    known = {i18n.L(*pair) for pair in PENDING_STATES} | {"…"}
    assert line in known, f"a built-in phrase was expected, got {line!r}"


def test_the_answer_lines_pass_through_the_skin(tmp_path, english, monkeypatch):
    """What the streaming renderer prints is what `on_stream` returned.

    Pieces are fed with their newlines: the renderer holds an unfinished line back
    on purpose, and a test that never completes a line would be testing the buffer
    rather than the surface.
    """
    from beeagent.ui.components import ResponseStream, thinking_line

    use("""
SURFACES = ("stream", "thinking")


def on_stream(piece, done):
    return "" if done else str(piece).upper()


def on_thinking(line):
    return "T:" + str(line)
""")
    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=60, force_terminal=False))
    renderer = ResponseStream()
    renderer.on_status()
    renderer.on_content("при" + "\n")
    renderer.on_content("вет" + "\n")
    renderer.on_done()
    printed = buffer.getvalue()
    assert "ПРИ" in printed and "ВЕТ" in printed, printed

    # The reasoning block only reaches the screen when the user opened it (F2),
    # so the surface itself is what is checked here, not the print.
    assert thinking_line("deep") == "T:deep"


def test_an_unclaimed_stream_line_is_the_model_s_own_text(tmp_path, english, monkeypatch):
    from beeagent.ui.components import ResponseStream

    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=60, force_terminal=False))
    renderer = ResponseStream()
    renderer.on_content("обычный текст" + "\n")
    renderer.on_done()
    assert "обычный текст" in buffer.getvalue(), buffer.getvalue()


# ------------------------------------------------- the same lines, on a terminal --
#
# Everything above is the TUI. The classic REPL is the interface a user gets over
# ssh, on Termux and on a cp1251 console, and it prints these lines through
# different objects — so each surface is checked in both, or a feature lands in one
# and reads as missing in the other.

def repl(monkeypatch, width: int = 60):
    """A streaming renderer writing into a buffer instead of a console."""
    from beeagent.ui.components import ResponseStream

    buffer = io.StringIO()
    monkeypatch.setattr("beeagent.ui.components.console",
                        Console(file=buffer, width=width, force_terminal=False))
    return ResponseStream(), buffer


def test_the_classic_repl_prints_the_rows_the_skin_drew(english, monkeypatch):
    from beeagent.ui.components import ResponseStream

    use("""
SURFACES = ("hud",)
HUD_ROWS = 1


def on_hud(painter, dt):
    painter.draw_text(0, 0, "HIVE ROW", color="#ffcc00")
""")
    renderer, buffer = repl(monkeypatch)
    renderer.on_status()
    printed = buffer.getvalue()
    assert "HIVE ROW" in printed, printed
    assert "[" not in printed, "the grid's colour leaked into the row as markup"


def test_markup_from_a_skin_renders_instead_of_spelling_itself_out(english, monkeypatch):
    """`[green]word[/]` is colour to Rich and garbage to a reader.

    A skin's answer to a text surface is markup, so the host has to parse it in
    both interfaces; the failure this prevents is a status line that prints its own
    style tags, which is what happens the moment one path appends the string raw.
    """
    use("""
SURFACES = ("status",)


def on_status(default):
    return "[#7cb342]busy[/] " + str(default)
""")
    renderer, buffer = repl(monkeypatch)
    renderer.on_status()
    printed = buffer.getvalue()
    assert "busy" in printed, printed
    assert "[#" not in printed and "[/]" not in printed, printed


def test_wording_that_is_not_markup_still_prints(english, monkeypatch):
    """A line whose tags do not parse is printed as the text it is, not lost.

    The host's own wording is escaped on the way in, but a plugin that fills the
    spinner slot writes whatever it likes, and a model name can hold a bracket.
    Rich raises on markup it cannot read; the line must not disappear with it.
    """
    from beeagent.ui.components import markup_text

    broken = markup_text("counting [cells] and [/more]")
    assert str(broken).startswith("counting [cells]"), repr(str(broken))
    tagged = markup_text("[#7cb342]leaf[/] line")
    assert str(tagged) == "leaf line", repr(str(tagged))


def test_a_skin_that_holds_the_answer_is_told_when_it_ends(english, monkeypatch):
    """`done` is a skin's last chance, or an animated answer loses its tail.

    The pack that types the reply out one letter per frame holds the text back;
    without a final call the last sentence never reaches the screen and the log
    keeps a shorter answer than the user was shown.
    """
    use("""
SURFACES = ("stream",)
HELD = ""


def on_stream(piece, done):
    global HELD
    HELD += str(piece)
    return "" if not done else HELD
""")
    renderer, buffer = repl(monkeypatch)
    renderer.on_content("held " + "\n")
    renderer.on_content("tail" + "\n")
    assert "held" not in buffer.getvalue(), "the skin said nothing and it printed anyway"
    renderer.on_done()
    printed = buffer.getvalue()
    assert "held" in printed and "tail" in printed, printed
    assert printed.count("tail") == 1, "the flush printed the tail twice"
