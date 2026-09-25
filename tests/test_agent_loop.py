"""The agent loop itself, end to end, against a provider that cannot reach a wire.

What is under test is what `Agent.run()` owes the user and the transcript:

  * `/stop` arming nothing while the agent is idle, and still being honoured in
    the window between the REPL setting `is_busy` and the task existing;
  * a tool answering in the wrong shape (a plugin returning a bare string) still
    landing a result row, so a saved transcript never ends on a call;
  * every dropped tool call reported, every time, to the model and to the user;
  * a cancelled turn saying what is actually true about an unstoppable worker;
  * the prompt-echo guard, which has to reject the prompt and not the history;
  * and the context-window measurements `core/windows.py` files per endpoint.

No test here can reach a provider: the fake is registered under exactly the name
the config selects, and every request is counted, so a stray call to the network
shows up as a wrong number rather than as a bill.

The source is ASCII on purpose: the console here is cp1251, and a failing test
should print, not raise a codec error.
"""
import asyncio
import json
import threading

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core import windows
from beeagent.core.agent import Agent, _ABANDONED, _gave_up_on_calls
from beeagent.core.parser import CommandParser
from beeagent.core.session import Session
from beeagent.i18n import set_lang
from beeagent.tools.base import BaseTool, ToolResult

PROVIDER = "fake"
MODEL = "fake-model"
UNSET = object()

# The sentence the loop used to write about a tool it stopped waiting for. The
# worker thread keeps going and may finish the work, so none of this is knowable.
FALSE_CANCEL_WORDS = ("before it finished", "before it could finish", "did not run",
                      "nothing was written", "was stopped", "terminated")


# --- the fake endpoint ------------------------------------------------------

class Fake:
    """Selectable under the configured name; hands over canned replies.

    It streams in chunks the way a real endpoint does, so the loop's own
    `_stream_response` is exercised rather than replaced.
    """

    name = PROVIDER
    models = [MODEL]
    supports_tools = False

    def __init__(self, replies=()):
        self.replies = list(replies)
        self.asked = []          # one entry per request that left the loop
        self.messages = []       # the message list of the newest request

    def _take(self, messages):
        self.asked.append(len(messages))
        self.messages = messages
        if not self.replies:
            raise AssertionError("the loop asked the model more times than scripted")
        reply = self.replies.pop(0)
        return reply(self, messages) if callable(reply) else reply

    async def chat(self, messages, model="", stream=False):
        return self._take(messages)

    async def chat_stream(self, messages, model=""):
        text = self._take(messages)
        for start in range(0, len(text), 7):
            yield ("content", text[start:start + 7])


def call_tool(tool, **args):
    """The reply shape the parser reads: one fenced json block."""
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


# --- test tools -------------------------------------------------------------

class NoteTool(BaseTool):
    """A tool whose answer can be set to anything an extension might return."""

    name = "note"
    description = "note something down"
    parameters = {"type": "object", "properties": {"text": {"type": "string"}},
                  "required": ["text"]}

    def __init__(self, answer=UNSET):
        self.ran = []
        self.answer = answer

    def is_safe(self):
        return True

    def execute(self, text=""):
        self.ran.append(text)
        if self.answer is UNSET:
            return ToolResult(output="noted: " + str(text), error=False)
        if callable(self.answer):
            return self.answer()
        return self.answer


class BlockerTool(BaseTool):
    """Still running when the turn is cancelled: the state cancel must describe."""

    name = "blocker"
    description = "waits to be let go"
    parameters = {"type": "object", "properties": {}}

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def is_safe(self):
        return True

    def execute(self):
        self.started.set()
        self.release.wait(10)
        return ToolResult(output="let go at last", error=False)


# --- fixtures and helpers ---------------------------------------------------

@pytest.fixture(autouse=True)
def _alone(tmp_path, monkeypatch):
    """A scratch workdir, an isolated windows cache, and a pinned language.

    `windows._CURRENT_PROVIDER` is process-wide on purpose -- the agent names the
    endpoint it selected, because ContextManager asks for a window by model name
    alone -- so a test must not inherit another module's idea of it.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(windows, "CACHE", tmp_path / ".beeagent" / "windows.json")
    monkeypatch.setattr(windows, "_CURRENT_PROVIDER", "")
    monkeypatch.setattr(windows, "_CACHED", None)
    monkeypatch.setattr(windows, "_CACHED_KEY", None)
    from beeagent import i18n

    previous = i18n.get_lang()
    set_lang("en")
    yield
    set_lang(previous)


def _agent(*replies, **config_kwargs):
    """An agent whose only endpoint is a scripted fake.

    Replies may be listed one by one or as one list, whichever reads better at
    the call site; a callable is asked to produce the reply, which is how a test
    presses `/stop` from inside a turn.
    """
    script = []
    for item in replies:
        if isinstance(item, list):
            script.extend(item)
        else:
            script.append(item)
    config_kwargs.setdefault("provider", PROVIDER)
    config_kwargs.setdefault("model", MODEL)
    config_kwargs.setdefault("permissions", {"mode": "auto"})
    agent = Agent(config=BeeConfig(**config_kwargs))
    fake = Fake(script)
    # Registered under the name the config selects; anything else and run() goes
    # looking for the real g4f endpoint.
    agent.providers.register(fake, replace=True)
    agent.tools.register(NoteTool(), replace=True)
    return agent, fake


def _events(sink):
    return lambda kind, data: sink.append((kind, data))


def _open_calls(session):
    """Assistant rows whose tool_calls were not all answered, in order.

    The transcript is saved and replayed on every later turn, so one row like
    this is a permanently malformed history.
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


def _rows(session, role=None):
    return [m.content for m in session.messages if role is None or m.role == role]


def _all(session):
    return " || ".join(f"{m.role}:{m.content}" for m in session.messages)


# ===========================================================================
# 1. /stop while nothing is running, and /stop in the race window
# ===========================================================================

def test_arming_a_stop_on_an_idle_agent_changes_nothing_at_all():
    agent, _ = _agent(["the real answer"])

    assert agent.request_stop() is False
    assert agent.stop_requested is False, "an idle /stop used to leave the flag armed"
    assert agent.is_busy is False


def test_the_question_after_an_idle_stop_is_asked_and_answered():
    """The cost of arming on idle was data: the next question was never asked."""
    agent, fake = _agent(["the real answer"])
    session = Session()
    events = []

    answer = asyncio.run(agent.run("what is 2+2?", session=session,
                                   callback=_events(events)))

    assert answer == "the real answer", answer
    assert "stopped" not in [kind for kind, _ in events], events
    assert len(fake.asked) == 1
    assert [(m.role, m.content) for m in session.messages] == \
           [("user", "what is 2+2?"), ("assistant", "the real answer")], \
        "a lone user row is the shape of a lost question"


def test_a_stop_armed_while_a_turn_runs_still_ends_that_turn():
    agent, fake = _agent(
        lambda provider, messages: (agent.request_stop(), call_tool("note", text="one"))[1],
        "never asked again",
    )
    events = []

    answer = asyncio.run(agent.run("do the thing", session=Session(),
                                   callback=_events(events)))

    assert answer == "Stopped by you", answer
    assert [kind for kind, _ in events].count("stopped") == 1
    assert agent.stop_requested is False, "the flag is cleared when the run is over"
    assert agent.is_busy is False


def test_a_stop_in_the_window_between_busy_and_the_task_is_honoured():
    """repl.py:353 sets `is_busy` synchronously and creates the task after it.

    That gap is a real turn the user is already waiting on, so a stop landing
    there arms and the run must never reach the model. Arming-on-idle and
    honouring-the-race-window are the same question only if `is_busy` is ignored:
    idle means there is no turn to stop, busy means there is one that may not
    have started yet.
    """
    agent, fake = _agent(["the whole answer nobody asked for"])

    agent.is_busy = True                      # what _spawn_agent_task() does first
    assert agent.request_stop() is True
    assert agent.stop_requested is True
    answer = asyncio.run(agent.run("hello", session=Session()))

    assert answer == "Stopped by you", answer
    assert fake.asked == [], "the model must not be asked once the user said stop"
    assert agent.is_busy is False and agent.stop_requested is False


def test_the_next_question_after_a_real_stop_is_answered():
    agent, fake = _agent(
        lambda provider, messages: (agent.request_stop(), call_tool("note", text="one"))[1],
        "here is the answer",
    )
    session = Session()
    assert asyncio.run(agent.run("do the thing", session=session)) == "Stopped by you"

    events = []
    again = asyncio.run(agent.run("carry on", session=session, callback=_events(events)))

    assert again == "here is the answer", again
    assert len(fake.asked) == 2, "one request per run: the stopped turn is not replayed"
    assert "stopped" not in [kind for kind, _ in events]
    assert _open_calls(session) == [], _all(session)
    assert agent.tools.get("note").ran == ["one"], "the stopped turn is not replayed"


# ===========================================================================
# 2. a tool that answers in the wrong shape
# ===========================================================================

@pytest.mark.parametrize("shape", [
    "noted: hello -- a bare string, the commonest plugin mistake",
    {"text": "noted", "ok": True},
    None,
    17,
    ToolResult(output="a proper result", error=False),
])
def test_a_tool_answering_in_any_shape_answers_its_call(shape):
    """`result.output` used to be read OUTSIDE the try.

    A plugin returning a string raised AttributeError that escaped run() after the
    assistant row carrying `tool_calls` had been written: the saved transcript
    ended on a call with no result, and a native provider replayed that malformed
    turn forever.
    """
    agent, fake = _agent([call_tool("note", text="hello"), "final answer"])
    agent.tools.get("note").answer = shape
    session = Session()
    events = []

    answer = asyncio.run(agent.run("note hello", session=session, callback=_events(events)))

    assert answer == "final answer", answer
    assert [kind for kind, _ in events].count("tool_end") == 1, events
    assert agent.is_busy is False
    assert _open_calls(session) == [], "the transcript cannot end on an unanswered call"
    tool_rows = _rows(session, "tool")
    assert tool_rows and tool_rows[0].startswith("[tool result] tool=note"), tool_rows
    if isinstance(shape, str):
        assert shape in tool_rows[0], "the string IS the output, not a crash"
    else:
        assert "error=False" in tool_rows[0], tool_rows[0]

    from beeagent.providers.crax import CraxProvider

    history = CraxProvider.to_openai_history(session.to_dicts())
    assert history[-1]["role"] == "assistant", history[-2:]
    pending = 0
    for row in history:
        pending += len(row.get("tool_calls") or [])
        if row.get("role") == "tool":
            pending = max(0, pending - 1)
    assert pending == 0, "a call without its result is replayed on every later turn"


def test_a_tool_that_raises_is_reported_as_a_failed_call():
    agent, fake = _agent([call_tool("note", text="hello"), "final answer"])

    def explode():
        raise RuntimeError("the extension died")

    agent.tools.get("note").answer = explode
    session = Session()

    assert asyncio.run(agent.run("note hello", session=session)) == "final answer"
    rows = _rows(session, "tool")
    assert any("the extension died" in row and "error=True" in row for row in rows), rows
    assert _open_calls(session) == []


def test_a_result_that_cannot_be_turned_into_text_still_gets_a_row():
    class Unreadable:
        def __str__(self):
            raise ValueError("no text for you")

    agent, fake = _agent([call_tool("note", text="hello"), "final answer"])
    agent.tools.get("note").answer = Unreadable()
    session = Session()
    events = []

    answer = asyncio.run(agent.run("note hello", session=session, callback=_events(events)))

    assert "could not read" in _all(session), _all(session)
    assert any(kind == "tool_end" and data.get("error") for kind, data in events), events
    assert _open_calls(session) == []
    assert answer == "final answer"


# ===========================================================================
# 3. dropped tool calls: reported every time, to the model and to the user
# ===========================================================================

CUT = '{"tool": "note", "args": {"text": "the second one is cut'


def test_a_dropped_call_beside_one_that_ran_is_reported_to_both():
    """"call A plus a truncated call B" used to run A and stay silent about B."""
    text = "Both notes at once.\n" + call_tool("note", text="first") + "\n```json\n" + CUT
    expected = CommandParser().parse(text).dropped
    assert expected, "the parser does report the cut-off call"

    agent, fake = _agent([text, "all done, both notes are saved"])
    note = agent.tools.get("note")
    session = Session()
    events = []

    asyncio.run(agent.run("take two notes", session=session, callback=_events(events)))

    assert note.ran == ["first"], "the call that arrived intact still runs"
    dropped = [data for kind, data in events if kind == "tool_dropped"]
    assert dropped, "a dropped call must be said out loud even when another ran"
    assert dropped[0]["notes"] == expected, "every dropped call of the turn is named"
    told = " ".join(_rows(session, "tool"))
    assert "arrived broken" in told, _all(session)
    assert json.dumps(fake.messages).count("arrived broken") == 1, \
        "and it reaches the model before the model answers"
    assert _open_calls(session) == []


def test_the_loop_forwards_every_note_the_parser_reported():
    """Not one-per-turn, not only-when-nothing-ran: all of them, every time."""
    text = "```json\n" + CUT
    expected = CommandParser().parse(text).dropped
    agent, fake = _agent([text, "carried on"])
    events = []

    asyncio.run(agent.run("take a note", callback=_events(events)))

    notes = [note for kind, data in events if kind == "tool_dropped" for note in data["notes"]]
    assert notes, "the parser reported a broken call"
    assert notes == expected, (notes, expected)


def test_two_cut_off_calls_in_a_row_are_never_sold_as_the_final_answer():
    """The second one used to come back as the answer, with `done` on it."""
    first = '```json\n{"tool": "note", "args": {"text": "first attempt, cut off mid'
    second = '```json\n{"tool": "note", "args": {"text": "second attempt, cut off mid'
    agent, fake = _agent([first, second, "must not be reached"])
    note = agent.tools.get("note")
    session = Session()
    events = []

    answer = asyncio.run(agent.run("take a note", session=session, callback=_events(events)))

    kinds = [kind for kind, _ in events]
    assert kinds.count("tool_dropped") == 2, "each drop is reported, not just the first"
    assert "done" not in kinds, kinds
    assert "error" in kinds, "the user learns there is no answer"
    assert answer == _gave_up_on_calls(), answer
    assert note.ran == []
    assert len(fake.asked) == 2, "the model was re-asked once, not until max_turns"
    assert _all(session).count("arrived broken") == 2, "and the model was told twice"
    assert _open_calls(session) == []


def test_a_model_that_recovers_after_a_broken_call_is_not_cut_off_by_that_budget():
    agent, fake = _agent(
        '```json\n{"tool": "note", "args": {"text": "cut off here',
        call_tool("note", text="the re-sent one"),
        "noted it",
    )
    note = agent.tools.get("note")
    events = []

    answer = asyncio.run(agent.run("take a note", callback=_events(events)))

    assert answer == "noted it", answer
    assert note.ran == ["the re-sent one"]
    assert [k for k, _ in events].count("tool_dropped") == 1
    assert "done" in [k for k, _ in events]


# ===========================================================================
# 4. what a cancelled turn is allowed to claim
# ===========================================================================

def _cancel_mid_tool(*replies):
    agent, fake = _agent(*replies)
    blocker = BlockerTool()
    agent.tools.register(blocker, replace=True)
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
    return agent, session, blocker


def test_a_cancelled_turn_says_the_result_was_abandoned_not_stopped():
    """The `to_thread` worker is unstoppable: it can finish after run() unwound."""
    agent, session, blocker = _cancel_mid_tool(call_tool("blocker"))
    rows = _rows(session, "tool")

    assert rows, "a cancelled turn still answers the call it carried"
    assert _ABANDONED in rows[0], rows[0]
    for phrase in FALSE_CANCEL_WORDS:
        assert phrase not in rows[0], f"the transcript claims the unknowable: {phrase}"
    # the honest half: the work may have landed, and the next turn must check
    assert "may still have finished" in rows[0], rows[0]
    assert _open_calls(session) == []
    assert agent.is_busy is False


def test_a_cancel_between_two_calls_answers_both():
    two = call_tool("blocker") + "\n" + call_tool("blocker")
    agent, session, blocker = _cancel_mid_tool(two)
    tool_rows = _rows(session, "tool")

    assert any(m.tool_calls and len(m.tool_calls) == 2 for m in session.messages)
    assert len(tool_rows) == 2, "one abandoned row per call still owed"
    assert all(_ABANDONED in row for row in tool_rows), tool_rows
    assert _open_calls(session) == [], "a half-answered turn is replayed forever"


# ===========================================================================
# 5. the prompt-echo guard
# ===========================================================================

QUOTE = ("The file starts with 'def main(): import sys' and then it defines a "
         "helper that parses argv, which is where the bug you asked about lives.")


def test_an_answer_that_quotes_what_a_tool_read_is_not_an_echo():
    """Six paid requests per question used to end in 'the provider returned an
    empty answer'."""
    agent, fake = _agent([QUOTE])
    session = Session()
    session.add_tool_result("[tool result] tool=read error=False\n" + QUOTE)
    events = []

    answer = asyncio.run(agent.run("what does the top of that file say?",
                                   session=session, callback=_events(events)))

    assert answer == QUOTE, answer
    assert len(fake.asked) == 1, "the answer was thrown away and re-asked"
    assert "retry" not in [kind for kind, _ in events], events
    assert not agent._is_prompt_echo(
        QUOTE, [{"role": "tool", "content": QUOTE},
                {"role": "user", "content": "what does the top of that file say?"}])


def test_an_answer_that_opens_by_restatement_is_not_an_echo():
    question = ("Explain how the agent loop delivers the messages queued while it "
                "was busy, and where the results go")
    agent, fake = _agent([question + " -- it drains them into the next request."])
    session = Session()
    session.add_user_message(question)

    answer = asyncio.run(agent.run("go", session=session))

    assert "it drains them" in answer, answer


def test_an_endpoint_that_recites_the_prompt_is_still_refused():
    """The guard must not become a no-op on the way to fixing the false positives."""
    from beeagent.core.context import SYSTEM_PROMPT

    agent, fake = _agent(lambda provider, messages: SYSTEM_PROMPT,
                         "the answer after the echo")
    session = Session()

    answer = asyncio.run(agent.run("hello", session=session))

    assert answer == "the answer after the echo", answer
    assert len(fake.asked) >= 2, "the recital was rejected and asked again"
    assert SYSTEM_PROMPT not in " ".join(_rows(session, "assistant")), \
        "the echo must not poison the history"


def test_the_guard_still_matches_a_verbatim_repeat_of_the_request():
    agent, _ = _agent([])
    request = ("do the task and check the result the agent produced, "
               "then show a short report of exactly what changed")
    assert agent._is_prompt_echo(request, [{"role": "user", "content": request}])
    assert not agent._is_prompt_echo("done", [{"role": "user", "content": request}])


def test_guest_mode_echo_markers_are_still_rejected():
    agent, _ = _agent([])
    sent = [{"role": "system", "content": "you have no file access at all"}]
    assert agent._is_prompt_echo(
        "OpenaiChat: Guest prompt: [SYSTEM: You are BeeCode, an autonomous agent", sent)
    assert not agent._is_prompt_echo("ok", sent)


# ===========================================================================
# 6. measuring a context window, and who a measurement belongs to
# ===========================================================================

class Named:
    """A provider with a name of its own: two of them can serve one model id."""

    supports_tools = False

    def __init__(self, name, reply="ok", refuses_at=None, limit=1500):
        self.name = name
        self.models = [MODEL]
        self.reply = reply
        self.refuses_at = refuses_at
        self.limit = limit
        self.sizes = []

    async def chat(self, messages, model="", stream=False):
        from beeagent.utils.tokens import count_tokens

        size = count_tokens(str(messages[0]["content"]), "gpt-4")
        self.sizes.append(size)
        if self.refuses_at and size > self.refuses_at:
            raise RuntimeError(f"maximum context length is {self.limit} tokens")
        return self.reply


def test_an_endpoint_that_answered_nothing_records_no_window():
    """A silent model used to measure as the top of the ladder: 131072."""
    endpoint = Named("silent-e", reply="")
    result = asyncio.run(windows.probe("tiny-model", endpoint,
                                       ceiling=windows.LADDER[-1], timeout=5, attempts=1))

    assert result.window is None, result.note
    assert "no answer at all" in result.note
    assert len(endpoint.sizes) == 1, "it stops at the first step, not all seven"
    assert windows.load() == {}, "nothing may be written for a silent endpoint"
    assert windows.measured("tiny-model", "silent-e") is None


def test_a_silent_answer_of_whitespace_counts_as_no_answer():
    result = asyncio.run(windows.probe("blank-model", Named("blank-e", reply="  \n "),
                                       ceiling=2048, timeout=5, attempts=1))
    assert result.window is None
    assert windows.load() == {}


def test_an_endpoint_that_really_answers_is_still_measurable():
    """The empty guard must not break the ladder it was written to protect."""
    endpoint = Named("narrow", refuses_at=1600, limit=1500)
    result = asyncio.run(windows.probe(MODEL, endpoint, ceiling=2048,
                                       timeout=5, attempts=1))
    assert result.window == 1500, result.note
    assert len(endpoint.sizes) == 1


def test_a_measurement_does_not_pin_another_provider_under_the_same_name():
    """Same model id, two endpoints, two windows: the number is not shareable."""
    asyncio.run(windows.probe(MODEL, Named("narrow", refuses_at=1600, limit=1500),
                              ceiling=2048, attempts=1, timeout=5))
    assert windows.load() == {f"{MODEL}@narrow": 1500}, windows.load()

    assert windows.measured(MODEL, "narrow") == 1500
    assert windows.measured(MODEL, "wide") is None, \
        "one endpoint's ceiling is not another's"

    from beeagent.core.context import window_for

    windows.note_provider("wide")
    assert windows.measured(MODEL) is None
    assert window_for(MODEL) != 1500, "a request must not be cut to a stranger's window"
    windows.note_provider("narrow")
    assert windows.measured(MODEL) == 1500
    assert window_for(MODEL) == 1500


def test_an_unattributed_record_still_answers_for_every_provider():
    """The cache format before this fix: bare model keys, kept readable."""
    windows.remember("legacy-model", 4096)
    assert windows.load() == {"legacy-model": 4096}
    assert windows.measured("legacy-model") == 4096
    windows.note_provider("whoever")
    assert windows.measured("legacy-model") == 4096
    assert windows.measured("legacy-model", "whoever") == 4096


def test_two_providers_measuring_one_model_name_keep_their_own_numbers():
    windows.remember("shared-model", 2048, "narrow")
    windows.remember("shared-model", 128000, "wide")
    assert windows.measured("shared-model", "narrow") == 2048
    assert windows.measured("shared-model", "wide") == 128000
    windows.note_provider("wide")
    assert windows.measured("shared-model") == 128000
    assert json.loads(windows.CACHE.read_text(encoding="utf-8")) == \
           {"shared-model@narrow": 2048, "shared-model@wide": 128000}


def test_the_agent_names_the_endpoint_it_chose():
    """ContextManager asks for a window by model name only, so run() has to say
    which endpoint it selected before the reads start answering."""
    agent, fake = _agent(["the answer"])
    windows.note_provider("")
    asyncio.run(agent.run("hello", session=Session()))

    assert windows.current_provider() == PROVIDER
