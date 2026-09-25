"""The TUI's clickable controls, driven headlessly and checked against state.

Every test boots the real app in a Textual pilot at 120x40, clicks a sidebar row
or a button or types into `#prompt`, and then reads what the click was *for*:
`config.model`, `config.provider`, `config.mode`, the agent's own context.
Nothing here asserts that a widget wrote something into the log.

No request leaves the process: the app runs on a plain local config with its
workdir pointed at a temporary directory (a chosen value is saved, and this
checkout is shared), and the command that would ask g4f for its catalogue is
handed a two-name list instead.
"""
import asyncio
import time
from contextlib import asynccontextmanager

from textual.color import Color
from textual.widgets import ListView

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Message, Session
from beeagent.ui import commands as ui_commands
from beeagent.ui.components import HONEY, LEAF
from beeagent.ui.tui import (
    HIVE_BACKDROP, HIVE_CURSOR, HIVE_PANEL, HIVE_ROW, BeeCodeApp, BeePicker,
)

SIZE = (120, 40)


@asynccontextmanager
async def running(tmp_path):
    """The real app, offline, with a workdir of its own."""
    app = BeeCodeApp(config=BeeConfig(), session=Session())
    app.agent.workdir = str(tmp_path)
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)        # on_mount: theme, sidebar, welcome, status
        yield app, pilot


async def type_line(pilot, app, line):
    """The mouse-free half of one path: the input, then Enter."""
    app.prompt.focus()
    app.prompt.value = line
    await pilot.pause(0.2)
    await pilot.press("enter")
    await pilot.pause(0.3)


def picker_rows(app):
    assert isinstance(app.screen, BeePicker), "no picker on screen"
    return {item._value: item
            for item in app.screen.query_one("#picker-list", ListView).children}


async def pick(pilot, app, value):
    await pilot.click(picker_rows(app)[value])
    await pilot.pause(0.3)


def sidebar_row(app, name):
    rows = app.home.query_one("#cmdlist", ListView).children
    return next(row for row in rows if getattr(row, "_cmd_name", "") == name)


async def settled_row(pilot, app, name, seconds=10.0):
    """The sidebar row for `name`, once the list has stopped changing.

    A keystroke in the prompt re-filters the command list, so a widget looked up
    before that render can be gone by the time the mouse event is delivered and
    the click lands on nothing. Waiting for the same widget twice in a row is the
    honest signal that this is the list the user is looking at; a row that never
    settles still fails, and says so.

    The failure this replaces was load-sensitive: alone, one of these three click
    tests failed per full run, and never the same one twice.
    """
    deadline = time.monotonic() + seconds
    previous = None
    while time.monotonic() < deadline:
        await pilot.pause()
        try:
            current = sidebar_row(app, name)
        except StopIteration:                # mid-render: the row is not there yet
            previous = None
            continue
        if current is previous:
            return current
        previous = current
    raise AssertionError(f"the sidebar row for {name!r} never stopped moving")


def log_text(app) -> str:
    return "\n".join(strip.text for strip in app.chatlog.lines)


def saved_config(tmp_path) -> dict:
    import json
    from pathlib import Path

    path = Path(tmp_path) / "beeagent.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


# --- the buttons -----------------------------------------------------------
def test_mode_button_changes_the_mode(tmp_path):
    """The button says "Mode"; the config is what has to move."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            await pilot.click("#mode")
            await pilot.pause(0.3)
            first = app.config.mode
            await pilot.click("#mode")
            await pilot.pause(0.3)
            return first, app.config.mode, app.agent.economy.mode, \
                saved_config(tmp_path)

    first, second, economy_mode, saved = asyncio.run(go())
    assert (first, second) == ("economy", "normal")
    assert economy_mode == "normal", "the agent runs in the mode the button set"
    assert saved.get("mode") == "normal", "and the choice survives a restart"


# --- pickers ---------------------------------------------------------------
def test_typed_mode_command_opens_a_picker_that_applies(tmp_path):
    """/mode with no argument used to print "current mode: normal" and stop."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/mode")
            rows = set(picker_rows(app))
            await pick(pilot, app, "economy")
            return rows, app.config.mode, app.agent.economy.mode, app.screen, \
                saved_config(tmp_path)

    rows, mode, economy_mode, screen, saved = asyncio.run(go())
    assert rows == {"normal", "economy"}
    assert mode == economy_mode == "economy"
    assert not isinstance(screen, BeePicker), "a choice closes the dialog"
    assert saved.get("mode") == "economy"


def test_provider_picker_moves_the_provider_and_the_model(tmp_path):
    """/providers listed the providers; choosing one was the part missing."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/providers")
            rows = set(picker_rows(app))
            await pick(pilot, app, "pool")
            pool = app.agent.providers.get("pool")
            return rows, app.config.provider, app.config.model, \
                app.agent.context.model, pool.models[0]

    rows, provider, model, context_model, expected = asyncio.run(go())
    assert rows == {"g4f", "pool"}
    assert provider == "pool"
    assert model == expected, "the model moves with the provider that can serve it"
    assert context_model == expected, "the running context agrees with the config"


def test_model_picker_applies_the_chosen_model(tmp_path, monkeypatch):
    """/models is the list a phone user taps instead of typing an id."""
    monkeypatch.setattr(ui_commands, "available_models",
                        lambda ctx, fetch=False: ["plain-model", "other-model"])

    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/models")
            rows = set(picker_rows(app))
            await pick(pilot, app, "other-model")
            return rows, app.config.model, app.agent.context.model

    rows, model, context_model = asyncio.run(go())
    assert rows == {"plain-model", "other-model"}
    assert model == context_model == "other-model"


def test_theme_picker_switches_the_interface_theme(tmp_path):
    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/theme")
            assert "nord" in picker_rows(app)
            await pick(pilot, app, "nord")
            return app.config.mode, app.ctx.theme, app._applied_theme

    mode, theme, applied = asyncio.run(go())
    assert theme == applied == "nord", "the chosen theme is the applied theme"
    assert mode == "normal", "a theme is not a mode: nothing else moved"


# --- the sidebar -----------------------------------------------------------
def test_sidebar_click_runs_a_command_without_a_second_enter(tmp_path):
    """A clicked row used to paste text into the input and stop there."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            app.prompt.focus()
            app.prompt.value = "/bee"
            await pilot.pause(0.2)
            before = app.ctx.bee_enabled
            await pilot.click(await settled_row(pilot, app, "bee"))
            await pilot.pause(0.3)
            return before, app.ctx.bee_enabled, app.prompt.value

    before, after, prompt = asyncio.run(go())
    assert (before, after) == (True, False), "the click ran /bee"
    assert prompt == "", "and did not leave the line waiting to be sent"


def test_sidebar_click_opens_the_same_picker(tmp_path):
    """One selection path: the row and the typed line end in the same dialog."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            app.prompt.focus()
            app.prompt.value = "/mode"
            await pilot.pause(0.2)
            await pilot.click(await settled_row(pilot, app, "mode"))
            await pilot.pause(0.3)
            rows = set(picker_rows(app))
            await pick(pilot, app, "economy")
            return rows, app.config.mode

    rows, mode = asyncio.run(go())
    assert rows == {"normal", "economy"}
    assert mode == "economy"


def test_a_command_needing_a_free_value_still_goes_to_the_input(tmp_path):
    """/read <path> has nothing to pick, so the click hands the typing over."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            app.prompt.focus()
            app.prompt.value = "/read"
            await pilot.pause(0.2)
            await pilot.click(await settled_row(pilot, app, "read"))
            await pilot.pause(0.3)
            return app.prompt.value, type(app.screen).__name__, app.config.mode

    value, screen, mode = asyncio.run(go())
    assert value == "/read " and screen != "BeePicker"
    assert mode == "normal", "nothing ran without its argument"


# --- cancelling ------------------------------------------------------------
def test_cancel_keeps_the_choice_unmade(tmp_path):
    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/mode")
            await pilot.click("#picker-cancel")
            await pilot.pause(0.3)
            cancelled = app.config.mode

            await type_line(pilot, app, "/mode")
            await pilot.press("escape")
            await pilot.pause(0.3)
            return cancelled, app.config.mode, type(app.screen).__name__

    cancelled, escaped, screen = asyncio.run(go())
    assert cancelled == escaped == "normal", "neither way out changed anything"
    assert screen != "BeePicker", "and both close the dialog"


# --- the brand rule --------------------------------------------------------
def test_the_picker_is_painted_in_the_bee_palette(tmp_path):
    """No default-styled dialog: hive backdrop, leaf frame, honey cursor."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            await type_line(pilot, app, "/mode")
            screen = app.screen
            listed = screen.query_one("#picker-list", ListView)
            return {
                "backdrop": screen.query_one("#picker-back").styles.background,
                "panel": screen.query_one("#picker").styles.background,
                "frame": screen.query_one("#picker").styles.border_top,
                "list": listed.styles.background,
                "row": listed.highlighted_child.styles.background,
                "row_text": listed.highlighted_child.styles.color,
                "button": screen.query_one("#picker-ok").styles.background,
                "hint": screen.query_one("#picker-hint").styles.color,
            }

    painted = asyncio.run(go())
    assert painted["backdrop"] == Color.parse(HIVE_BACKDROP)
    assert painted["panel"] == Color.parse(HIVE_PANEL)
    assert painted["list"] == Color.parse(HIVE_BACKDROP)
    assert painted["row"] == Color.parse(HIVE_CURSOR), "the cursor is leaf green"
    assert painted["row_text"] in (Color.parse(HONEY), Color.parse("#ffffff"))
    assert painted["button"] == Color.parse(HIVE_PANEL)
    assert painted["hint"] == Color.parse(HIVE_ROW)
    assert painted["frame"][1] == Color.parse(LEAF), "the frame is the leaf border"


# --- the actions that were only printed ------------------------------------
def test_history_command_shows_the_conversation(tmp_path):
    """/history promised "scroll with the wheel" and opened nothing."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            app.ctx.session.messages.append(Message("user", "pelmeni-recipe"))
            before = log_text(app)
            await type_line(pilot, app, "/history")
            return before, log_text(app)

    before, after = asyncio.run(go())
    assert "pelmeni-recipe" not in before
    assert "pelmeni-recipe" in after


def test_update_command_reaches_the_updater(tmp_path, monkeypatch):
    """/update printed "found 6.0.1 — updating" and updated nothing."""
    from beeagent.core import updater

    monkeypatch.setattr(updater, "check", lambda **kwargs: {"latest": "99.9.9"})
    monkeypatch.setattr(updater, "is_newer", lambda latest, current: True)

    async def go():
        async with running(tmp_path) as (app, pilot):
            started = []
            app._run_update = lambda: started.append("update")
            await type_line(pilot, app, "/update")
            return started

    started = asyncio.run(go())
    assert started == ["update"], "the announced update is handed to the updater"


def test_stop_command_reaches_the_agent(tmp_path):
    """`/stop` while a turn is running arms the flag; while idle it must not."""
    async def go():
        async with running(tmp_path) as (app, pilot):
            before = app.agent.stop_requested
            await type_line(pilot, app, "/stop")
            idle = app.agent.stop_requested
            app.agent.is_busy = True            # a turn exists, as far as the UI knows
            await type_line(pilot, app, "/stop")
            return before, idle, app.agent.stop_requested

    before, idle, armed = asyncio.run(go())
    assert before is False and idle is False, (
        "an idle /stop armed the flag and the NEXT question was never asked")
    assert armed is True, "with a turn running, /stop reaches the loop"
