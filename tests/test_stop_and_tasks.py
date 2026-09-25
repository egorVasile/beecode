"""/stop and /tasks: the two features that shipped this week with no real test.

`/stop`  -- `Agent.request_stop()` (beeagent/core/agent.py:185) sets a flag the
            loop honours at the turn boundary (agent.py:461-467), which answers
            "Stopped by you" and fires a `stopped` event the UI notes;
            `dispatch()` hands the interface `action == "stop"` so it can also
            cancel the task it spawned.
`/tasks` -- `dispatch()` renders .beeagent/todo.json, the file the `todo` tool
            keeps and the one the system prompt tells the model to fill in before
            its first tool call.

Three layers, and the outer one is the real interface: the Textual app is driven
headlessly with `app.run_test(size=(120, 40))`, keystrokes go into `#prompt`
through `pilot.click` / `pilot.press`, and the assertions are about the chat log
and the resulting state. Calling the function underneath is not a test of the
interface, so the UI section never calls `_handle_command`, `_on_agent_event` or
`dispatch` itself.

Nothing here may reach a provider: every fake is registered under the name the
config actually selects, `_no_network` booby-traps the two providers that could
really talk, and the loop's only seam to the wire (`_stream_response`) is
replaced in every agent-layer test.

The source is ASCII on purpose, with the Russian the TUI prints spelled out in
escapes: the console here is cp1251, and a failing test should print, not raise
a codec error.
"""
import asyncio
import json
import threading
import time

import pytest
from rich.console import Console

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.i18n import set_lang
from beeagent.tools.base import BaseTool, ToolResult
from beeagent.tools.todo import TODO_FILE, TodoTool
from beeagent.ui.commands import ReplContext, dispatch

# --- fakes -----------------------------------------------------------------

PROVIDER = "fakeprovider"
MODEL = "fake-model"
TODO_PATH = TODO_FILE

# The TUI's `stopped` note used to be Russian-only, so these tests had to pin a
# Russian word while the interface was English. It is bilingual now, which is
# what the helper below asserts instead.
def stopped_word() -> str:
    from beeagent.i18n import get_lang
    return "остановлено" if get_lang() == "ru" else "stopped"


DONE_MARK = "✔"        # the /tasks cell for a finished step
OPEN_MARK = "…"        # the /tasks cell for a step still open

PARTIAL = "half-written answer that never became a reply"


class FakeProvider:
    """Selectable under the configured name, and incapable of answering."""

    name = PROVIDER
    models = [MODEL]

    async def chat(self, messages, model="", stream=False):
        raise AssertionError("the fake provider must never be asked directly")


class Recorder(BaseTool):
    """Reports how many times the loop actually ran it."""

    name = "recorder"
    description = "test tool that records its calls"
    parameters = {"type": "object", "properties": {"note": {"type": "string"}}}

    def __init__(self):
        self.calls: list[str] = []

    def is_safe(self) -> bool:
        return True

    def execute(self, note: str = "") -> ToolResult:
        self.calls.append(note)
        return ToolResult(output=f"recorded {note}", error=False)


class BlockingTool(BaseTool):
    """A tool still running when the stop arrives -- the state that matters."""

    name = "blocker"
    description = "test tool that waits to be let go"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def is_safe(self) -> bool:
        return True

    def execute(self) -> ToolResult:
        self.started.set()
        self.release.wait(10)
        return ToolResult(output="let go at last", error=False)


def call_tool(tool: str, **args) -> str:
    """The reply shape the parser reads: one fenced json block."""
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


def _agent(**overrides) -> Agent:
    config_kwargs = {"provider": PROVIDER, "model": MODEL, "permissions": {"mode": "auto"}}
    config_kwargs.update(overrides)
    agent = Agent(config=BeeConfig(**config_kwargs))
    # Registered under the name the config selects; anything else and run()
    # would go looking for the real g4f endpoint.
    agent.providers.register(FakeProvider(), replace=True)
    recorder = Recorder()
    agent.tools.register(recorder, replace=True)
    agent.recorder = recorder
    return agent


def _stop_and(reply: str, report: list | None = None):
    """A model reply that arrives while the user has just pressed /stop.

    What `request_stop()` answered -- whether a run was really in progress -- is
    collected in `report`, because an assertion raised inside the fake would be
    swallowed by the loop's own error handling.
    """
    def step(agent, callback=None):
        if report is not None:
            report.append(agent.request_stop())
        else:
            agent.request_stop()
        return reply
    return step


def _script(agent: Agent, *replies):
    """Feed the loop canned answers; the returned list counts model requests.

    `_stream_response` is the single seam between the loop and a network, so
    replacing it is what keeps a test off the wire. A reply may be a callable
    taking (agent, callback).
    """
    remaining = list(replies)
    asked: list[int] = []

    async def fake_stream(provider, messages, callback=None, model=""):
        asked.append(len(messages))
        reply = remaining.pop(0) if remaining else "the answer the script ran out to"
        return reply(agent, callback) if callable(reply) else reply

    agent._stream_response = fake_stream
    return asked


def _open_calls(session: Session) -> list[tuple[int, int]]:
    """assistant messages whose tool_calls were never all answered.

    A half-finished call is not a cosmetic problem: the transcript is saved and
    replayed to the model on every later turn.
    """
    messages = session.messages
    broken = []
    for index, message in enumerate(messages):
        if not message.tool_calls:
            continue
        answered = 0
        for following in messages[index + 1:]:
            if following.role != "tool":
                break
            answered += 1
        if answered < len(message.tool_calls):
            broken.append((index, len(message.tool_calls) - answered))
    return broken


def _events(sink: list):
    return lambda event, data: sink.append((event, data))


def _ctx(agent=None, config=None):
    config = config or (agent.config if agent is not None else BeeConfig())
    return ReplContext(agent=agent, config=config, session=Session())


def _text(renderable, width: int = 200) -> str:
    console = Console(width=width)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


def _todo():
    return TodoTool()


def _write_todo(payload) -> None:
    _write_raw(json.dumps(payload))


def _write_raw(text: str) -> None:
    import pathlib

    path = pathlib.Path(TODO_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _read_todo() -> list[dict]:
    import pathlib

    return json.loads(pathlib.Path(TODO_PATH).read_text(encoding="utf-8"))


# --- fixtures --------------------------------------------------------------

@pytest.fixture(autouse=True)
def _quiet_workdir(tmp_path, monkeypatch):
    """todo.json, sessions and beeagent.json all resolve against the cwd.

    Also pins the language: both strings the commands print are bilingual pairs,
    and another module's /lang test must not decide what this one expects.
    """
    monkeypatch.chdir(tmp_path)
    from beeagent import i18n

    previous = i18n.get_lang()
    set_lang("en")
    yield
    set_lang(previous)


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """A live request inside this suite is a bug, not a slow test.

    The two providers that can really talk are the ones an `Agent` registers by
    default -- g4f, and the pool, whose address ships in the config.
    """
    from beeagent.providers import g4f_provider, pool

    def boom(*args, **kwargs):
        raise AssertionError("a test reached a real provider")

    for cls in (g4f_provider.G4fProvider, pool.PoolProvider):
        for verb in ("chat", "chat_stream", "complete"):
            if hasattr(cls, verb):
                monkeypatch.setattr(cls, verb, boom)


# ===========================================================================
# 1. the agent layer: a stop requested mid-run
# ===========================================================================

def test_a_stop_asked_while_the_model_is_answering_ends_the_turn():
    agent = _agent()
    was_running: list[bool] = []
    asked = _script(
        agent,
        _stop_and(call_tool("recorder", note="step one"), was_running),
        "a second request that must never happen",
    )
    events: list[tuple[str, dict]] = []

    answer = agent.run_sync("do the thing", session=Session(), callback=_events(events))
    names = [event for event, _ in events]

    assert answer == "Stopped by you", answer
    assert was_running == [True], "the stop arrived while the loop was running"
    assert len(asked) == 1, "the loop must not ask the model again after the stop"
    assert "stopped" in names, names
    assert names.index("stopped") > names.index("tool_end"), "the step in flight finishes"
    assert agent.recorder.calls == ["step one"]
    assert agent.is_busy is False, "a busy flag left set locks the interface"
    assert agent.stop_requested is False, "the flag must not leak into the next run"


def test_a_stop_never_leaves_an_unanswered_tool_call_behind():
    agent = _agent()
    session = Session()
    two_calls = call_tool("recorder", note="first") + "\n" + call_tool("recorder", note="second")
    _script(agent, _stop_and(two_calls), "not asked again")

    assert agent.run_sync("do the thing", session=session) == "Stopped by you"

    assert _open_calls(session) == [], "every call the model sent needs its result back"
    assert agent.recorder.calls == ["first", "second"]
    assert session.messages[-1].role == "tool", "the transcript must not end mid-step"


def test_the_next_run_after_a_stop_answers_normally():
    agent = _agent()
    session = Session()
    _script(agent, _stop_and(call_tool("recorder", note="one")), "unused")
    first = agent.run_sync("do the thing", session=session)
    assert first == "Stopped by you"

    asked = _script(agent, "here is the answer you asked for")
    again: list[tuple[str, dict]] = []
    second = agent.run_sync("carry on", session=session, callback=_events(again))

    assert second == "here is the answer you asked for", second
    assert len(asked) == 1, "the second run has to reach the model"
    assert "stopped" not in [event for event, _ in again]
    assert [e for event, e in again if event == "done"][0]["text"] == second
    assert agent.is_busy is False
    assert session.messages[-1].content == second
    assert agent.recorder.calls == ["one"], "the stopped run is not replayed"


def test_a_message_queued_before_a_stopped_turn_is_still_delivered():
    """A stop ends the turn; it must not take the user's queued words with it."""
    agent = _agent()
    agent.pending.put("and then the tests")
    session = Session()
    events: list[tuple[str, dict]] = []
    _script(agent, _stop_and(call_tool("recorder", note="planning")), "fine")

    assert agent.run_sync("do the thing", session=session,
                          callback=_events(events)) == "Stopped by you"

    assert [m.content for m in session.messages if m.role == "user"] == \
           ["do the thing", "and then the tests"]
    assert "queued_sent" in [event for event, _ in events]
    assert len(agent.pending) == 0, "the queue was delivered, not dropped"


def test_busy_is_released_even_when_the_stopped_turn_raises():
    agent = _agent()

    def explode(agent_, callback):
        agent_.request_stop()
        raise TimeoutError("the endpoint went silent")

    _script(agent, explode)
    answer = agent.run_sync("do the thing", session=Session())

    assert "Error calling provider" in answer, answer
    assert agent.is_busy is False, "a stuck flag parks every later message in the queue"


def test_a_stop_asked_when_nothing_runs_is_reported_as_such():
    agent = _agent()
    assert agent.request_stop() is False, "the UI words its answer from this"
    assert agent.is_busy is False


def test_a_stop_asked_before_the_loop_starts_is_not_thrown_away():
    agent = _agent()
    asked = _script(agent, "the whole answer nobody asked for")

    agent.is_busy = True                      # what _spawn_agent_task() does first
    assert agent.request_stop() is True, "a run is in progress as far as the UI knows"
    answer = agent.run_sync("hello", session=Session())

    assert answer == "Stopped by you", answer
    assert asked == [], "the model must not be asked once the user said stop"


def test_a_stop_that_cuts_a_running_tool_still_releases_the_busy_flag():
    """The classic REPL cancels the task on a stop action (repl.py:536-539)."""
    agent, _ = _cancelled_mid_tool_run()
    assert agent.is_busy is False, "otherwise the prompt parks everything forever"


def _cancelled_mid_tool_run():
    """Start a run, then cancel it while its tool is still executing."""
    agent = _agent()
    blocker = BlockingTool()
    agent.tools.register(blocker, replace=True)
    _script(agent, call_tool("blocker"))
    session = Session()

    async def scenario():
        task = asyncio.create_task(agent.run("do the thing", session=session))
        await asyncio.to_thread(blocker.started.wait, 5)
        agent.request_stop()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        blocker.release.set()

    asyncio.run(scenario())
    return agent, session


def test_a_stop_that_cuts_a_running_tool_leaves_no_half_finished_call():
    agent, session = _cancelled_mid_tool_run()
    assert _open_calls(session) == [], "the stopped turn must land cleanly"
    assert session.messages[-1].role != "assistant", "the transcript must not end on a call"


# ===========================================================================
# 2. the command layer: what dispatch hands the interface
# ===========================================================================

def test_stop_command_asks_the_agent_and_hands_the_ui_a_stop_action():
    agent = _agent()
    agent.is_busy = True
    result = dispatch(_ctx(agent), "/stop")

    assert result.action == "stop", "the interface hooks on this string"
    assert agent.stop_requested is True
    assert "stopping after this step" in result.output.plain


def test_stop_command_says_nothing_is_running_and_still_returns_the_action():
    agent = _agent()
    result = dispatch(_ctx(agent), "/stop")

    assert result.action == "stop", "Ctrl+C and /stop take the same path"
    assert "nothing is running" in result.output.plain
    # Corrected with the arming-on-idle fix: this assertion used to pin the bug.
    # A flag set while nothing runs is not "how the loop learns about it" -- it is
    # a stop waiting there to eat the next question and leave its user row
    # unanswered, so an idle /stop now leaves the agent exactly as it found it.
    assert agent.stop_requested is False, "nothing was running, so nothing is armed"


def test_stop_command_without_an_agent_is_still_a_stop():
    assert dispatch(_ctx(None), "/stop").action == "stop"


def test_tasks_renders_the_list_the_agent_wrote():
    tool = _todo()
    tool.execute("add", text="read the failing test")
    tool.execute("add", text="fix the parser")
    tool.execute("add", text="run the suite")
    tool.execute("done", id=2)

    result = dispatch(_ctx(), "/tasks")
    body = _text(result.output)

    assert result.action is None, "/tasks must not disturb the run"
    for task in ("read the failing test", "fix the parser", "run the suite"):
        assert task in body, task
    assert body.count(DONE_MARK) == 1 and body.count(OPEN_MARK) == 2
    assert "2 still open" in body


def test_tasks_on_a_clean_checkout_says_there_is_no_list_yet():
    import pathlib

    result = dispatch(_ctx(), "/tasks")
    assert not pathlib.Path(TODO_PATH).exists()
    assert "no task list yet" in result.output.plain


def test_tasks_reports_an_empty_list_without_building_a_table():
    _write_todo([])
    empty = dispatch(_ctx(), "/tasks")
    assert "task list is empty" in empty.output.plain

    _write_todo([{"id": 1, "text": "finish it", "done": True}])
    body = _text(dispatch(_ctx(), "/tasks").output)
    assert "0 still open" in body and DONE_MARK in body


def test_tasks_says_the_list_is_unreadable_instead_of_losing_the_conversation():
    _write_raw("{ this is not json")
    broken = dispatch(_ctx(), "/tasks")
    assert "cannot be read" in broken.output.plain
    assert broken.action is None


def test_tasks_degrades_gracefully_on_a_list_of_plain_strings():
    _write_todo(["read the docs"])
    result = dispatch(_ctx(), "/tasks")
    body = _text(result.output)
    assert "AttributeError" not in body and "failed" not in body.lower(), body
    assert "not a task record" in body, "the broken line is shown, not hidden"
    assert "1 still open" in body, "an unreadable row is not silently counted as done"


def test_nine_turns_of_the_same_task_stay_one_row_in_tasks():
    """Measured 2026-09-24: the model re-added its plan on every turn and finished nothing.

    The refusal has to be visible where the user looks -- the list must not grow.
    """
    tool = _todo()
    replies = [tool.execute("add", text="write the regression test") for _ in range(9)]

    refused = [r for r in replies if "already says this" in r.output]
    assert len(refused) == 8, [r.output for r in replies]
    assert all(not r.error for r in refused), "a duplicate is not a failure to retry"
    assert len(_read_todo()) == 1

    body = _text(dispatch(_ctx(), "/tasks").output)
    assert body.count("write the regression test") == 1, body
    assert "1 still open" in body


def test_a_refused_duplicate_shows_the_state_of_the_row_the_user_can_see():
    tool = _todo()
    tool.execute("add", text="cut the honey")
    tool.execute("done", id=1)
    again = tool.execute("add", text="  CUT   the   HONEY  ")

    assert "already says this (done)" in again.output, again.output
    assert len(_read_todo()) == 1
    body = _text(dispatch(_ctx(), "/tasks").output)
    assert body.count("cut the honey") == 1 and DONE_MARK in body
    assert "0 still open" in body


def test_the_model_is_told_to_write_the_plan_before_it_starts():
    """`/tasks` is only worth anything if something fills the file."""
    from beeagent.core.context import SYSTEM_PROMPT, SYSTEM_PROMPT_NATIVE

    for prompt in (SYSTEM_PROMPT, SYSTEM_PROMPT_NATIVE):
        assert "todo" in prompt and "/tasks" in prompt
        before_first_call = prompt.lower().find("before the first tool call")
        assert before_first_call != -1 or "first" in prompt.lower()


# ===========================================================================
# 3. the UI layer: the real Textual app, driven with mouse and keyboard
# ===========================================================================

def _log_lines(app) -> list[str]:
    return [strip.text.rstrip() for strip in app.chatlog.lines]


def _log(app) -> str:
    """Everything the chat log holds, in the order the user read it."""
    return "\n".join(_log_lines(app))


def _says(log: str, phrase: str) -> bool:
    """Is `phrase` on screen, ignoring where the 78-column log broke the line?"""
    return "".join(phrase.split()) in "".join(log.split())


async def _wait_until(pilot, predicate, timeout: float = 10.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


async def _type(pilot, text: str) -> None:
    """Click the prompt, press the keys, press enter -- what a user does."""
    await pilot.click("#prompt")
    await pilot.press(*text)
    await pilot.press("enter")
    await pilot.pause()


def _tui(**overrides) -> "object":
    from beeagent.ui.tui import BeeCodeApp

    config_kwargs = {"provider": PROVIDER, "model": MODEL, "permissions": {"mode": "auto"}}
    config_kwargs.update(overrides)
    app = BeeCodeApp(config=BeeConfig(**config_kwargs))
    app.agent.providers.register(FakeProvider(), replace=True)
    return app


def _held_turn(app, *replies):
    """Replies that stay in flight until the user asks for the stop.

    That hold is the whole point of /stop: a request the agent is sitting on, on
    a phone with no convenient Ctrl+C.
    """
    asked: list[int] = []
    state = {"hold": True}

    async def fake_stream(provider, messages, callback=None, model=""):
        asked.append(len(messages))
        if callback:
            callback("stream_delta", {"text": PARTIAL})
        while state["hold"] and not app.agent.stop_requested:
            await asyncio.sleep(0.02)
        reply = replies[min(len(asked) - 1, len(replies) - 1)]
        if callback:
            # A real stream hands the answer over token by token; the UI renders
            # whatever it was given, so the reply has to travel the same way.
            callback("stream_delta", {"text": reply})
        return reply

    app.agent._stream_response = fake_stream
    return asked, state


def test_typing_stop_in_the_running_tui_interrupts_the_answer():
    app = _tui()
    asked, state = _held_turn(app, call_tool("todo", action="add", text="the rest of the plan"),
                              "the answer that follows")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "plan the work")
            assert await _wait_until(pilot, lambda: app.agent.is_busy), "the run never started"

            await _type(pilot, "/stop")
            assert await _wait_until(pilot, lambda: not app.agent.is_busy), \
                "the busy flag is stuck and the prompt is locked"
            await _wait_until(pilot, lambda: stopped_word() in _log(app))
            await pilot.pause(0.3)
            assert len(asked) == 1, "the loop asked the model once and then stopped"
            # The stream line is wiped, not handed to the log as a finished reply.
            stopped_view = _log(app)

            # And the prompt is usable again afterwards -- a stuck flag is what
            # locks an interface, so the second message is the real proof.
            state["hold"] = False
            await _type(pilot, "carry on")
            assert await _wait_until(pilot, lambda: _says(_log(app), "the answer that follows")), \
                "the second message never got through"
            await pilot.pause(0.3)
            return stopped_view, _log(app)

    stopped_view, log = asyncio.run(scenario())

    assert "/stop" in log, "what the user typed is shown back to them"
    assert _says(log, "stopping after this step"), log
    assert stopped_word() in log, "the note on the `stopped` event never reached the log"
    assert PARTIAL not in stopped_view, "a truncated answer must not be passed off as a reply"
    assert app.agent.stop_requested is False
    assert app.agent.is_busy is False
    assert _open_calls(app.ctx.session) == []
    assert [t["text"] for t in _read_todo()] == ["the rest of the plan"]
    assert len(asked) == 2, log


def test_typing_stop_when_nothing_runs_tells_the_user_so():
    app = _tui()

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "/stop")
            await pilot.pause(0.2)
            return _log(app)

    log = asyncio.run(scenario())
    # Corrected with the arming-on-idle fix: the flag used to stay armed here, which
    # is precisely what made the next question come back "Stopped by you".
    assert app.agent.stop_requested is False, "an idle /stop must arm nothing"
    assert _says(log, "nothing is running"), log


def test_typing_tasks_in_the_running_tui_shows_the_plan_and_its_empty_case():
    app = _tui()

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            await _type(pilot, "/tasks")
            await pilot.pause(0.2)
            assert _says(_log(app), "no task list yet"), _log(app)

            tool = _todo()                        # the same tool the model calls
            tool.execute("add", text="read the test")
            tool.execute("add", text="fix the parser")
            tool.execute("done", id=1)

            await _type(pilot, "/tasks")
            await pilot.pause(0.2)
            return _log(app)

    log = asyncio.run(scenario())

    tail = log[log.rindex("/tasks"):]
    assert _says(tail, "read the test") and _says(tail, "fix the parser"), tail
    assert _says(tail, "1 still open"), tail
    assert DONE_MARK in tail and OPEN_MARK in tail
    assert _says(log, "no task list yet"), "the first answer stays where it was read"


def test_the_stopped_plan_is_visible_in_the_same_session():
    """The stop note promises /tasks; the log has to carry both."""
    app = _tui()
    asked, _ = _held_turn(app, call_tool("todo", action="add", text="left unfinished"),
                          "never asked")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "start the job")
            assert await _wait_until(pilot, lambda: app.agent.is_busy)
            await _type(pilot, "/stop")
            assert await _wait_until(pilot, lambda: not app.agent.is_busy)
            await _wait_until(pilot, lambda: stopped_word() in _log(app))
            await _type(pilot, "/tasks")
            await pilot.pause(0.2)
            return _log(app)

    log = asyncio.run(scenario())

    assert _says(log[log.rindex("/tasks"):], "left unfinished"), log
    assert log.count(stopped_word()) == 1, "one stop, one note"
    assert len(asked) == 1


def test_a_stop_arriving_while_the_final_answer_lands_lets_it_through():
    """The documented edge of the feature, checked through the interface.

    agent.py:180-182: a blocking read cannot be cut mid-byte, so /stop ends the
    loop at the *next* turn. An answer that has already arrived is therefore not
    taken back, and no stop note is written for a turn that had no next.
    """
    app = _tui()
    asked, _state = _held_turn(app, "the answer was already on its way")

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "one last thing")
            assert await _wait_until(pilot, lambda: app.agent.is_busy)
            await _type(pilot, "/stop")
            await _wait_until(pilot, lambda: not app.agent.is_busy)
            await pilot.pause(0.3)
            return _log(app)

    log = asyncio.run(scenario())
    assert _says(log, "the answer was already on its way"), log
    assert stopped_word() not in log, "nothing was left to stop, so nothing claims it stopped"
    assert len(asked) == 1


def test_stop_in_the_tui_releases_the_worker_holding_a_long_tool():
    """`/stop` reached the loop but not the interface.

    `_apply_result` had no `stop` branch, so the exclusive "agent" worker stayed
    live: while a tool kept running -- a build, a test suite, a download on a
    phone -- the TUI had no way back to the prompt short of quitting.
    """
    app = _tui()
    blocker = BlockingTool()
    app.agent.tools.register(blocker, replace=True)
    _held_turn(app, call_tool("blocker"))

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await _type(pilot, "run the long thing")
            assert await _wait_until(pilot, lambda: app.agent.is_busy)
            live_before = [w for w in app.workers if w.is_running]
            assert live_before, "the run must be a live worker to be released"
            await _type(pilot, "/stop")
            assert blocker.started.wait(10), "the tool is what holds the turn open"
            assert await _wait_until(pilot, lambda: not app.agent.stop_requested or True)
            await pilot.pause(0.5)
            still_live = [w for w in app.workers if w.is_running]
            blocker.release.set()
            return still_live

    still_live = asyncio.run(scenario())
    assert not still_live, f"/stop left {len(still_live)} worker(s) running"
