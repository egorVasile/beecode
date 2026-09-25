"""What the user is told when the loop has to fix, drop or throw something away.

`beeagent/ui/tui.py:_on_agent_event` is the default interface, and eight of the
events the agent loop emits had no branch there at all: `stream_reset`,
`context_trimmed`, `waiting`, `tool_dropped`, `tool_repaired`, `tool_renamed`,
`tool_unknown`, `nudged`. A session could run on a model whose calls were being
repaired, re-asked and trimmed the whole time, and the screen stayed clean while
the transcript got shorter -- the proof being a turn that showed "half an answer…
the real answer" glued together, because nothing told the UI to forget the
fragment.

Every test here drives the real Textual app: `run_test(size=(120, 40))`, a click
on `#prompt`, the keys a person types, and then the chat log is read the way the
user reads it (and the status label the way they glance at it). The agent loop is
the real one; only the endpoint is a script, registered under the name the config
selects. Assertions are about the note on screen and about the answer text next
to it -- never about a function having been called.

The source is ASCII on purpose: this console is cp1251, and a failing test has to
print, not raise a codec error. The Russian half of each note is therefore
checked by character range, not by literal.
"""
import asyncio
import json
import re
import time

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Message, Session
from beeagent.i18n import get_lang, set_lang
from beeagent.ui.tui import BeeCodeApp

SIZE = (120, 40)
PROVIDER = "fakeevents"
MODEL = "fake-model"

DONE = "all finished here"
PARTIAL = "PARTIAL FRAGMENT that never became an answer"
CLEAN = "FALLBACK-CLEAN-ANSWER"
RUN_MARK = "\u23f3"        # the hourglass the TUI puts in front of a running tool
STOP_MARK = "\U0001f6d1"   # and the stop sign on the `stopped` note
CYRILLIC = re.compile(r"[\u0400-\u04ff]")


# --- fakes -----------------------------------------------------------------

class Scripted:
    """A provider that answers from a script, one item per turn it is asked.

    An item is either the text the stream sends, or a dict:
      {"chunks": [(kind, text), ...], "stall": seconds, "die_after": text}
    `die_after` is the proven failure: the stream says part of an answer and then
    breaks, so the loop falls back to `chat()` and the UI has to abandon what it
    was already showing. `stall` is the silent endpoint the heartbeat exists for.
    """

    name = PROVIDER
    models = [MODEL]

    def __init__(self, script):
        self.script = list(script)
        self.asked = 0

    async def chat_stream(self, messages, model=""):
        self.asked += 1
        item = self.script[min(self.asked - 1, len(self.script) - 1)]
        if isinstance(item, dict):
            if item.get("stall"):
                await asyncio.sleep(item["stall"])
            if item.get("die_after"):
                yield ("content", PARTIAL)
                raise RuntimeError("the stream broke mid-sentence")
            for chunk in item.get("chunks", []):
                yield chunk
            return
        yield ("content", item)

    async def chat(self, messages, model=""):
        return CLEAN


# --- harness ---------------------------------------------------------------

@pytest.fixture(autouse=True)
def _offline(tmp_path, monkeypatch):
    """No network, no folder-trust prompt, and a pinned language.

    `set_lang` is global, so a bilingual note can only be compared against one
    half at a time; leaving it to whatever another module set would make these
    assertions depend on test order.
    """
    monkeypatch.chdir(tmp_path)
    from beeagent.providers import g4f_provider, pool

    def boom(*args, **kwargs):
        raise AssertionError("a test reached a real provider")

    for cls in (g4f_provider.G4fProvider, pool.PoolProvider):
        for verb in ("chat", "chat_stream", "complete"):
            if hasattr(cls, verb):
                monkeypatch.setattr(cls, verb, boom)

    previous = get_lang()
    set_lang("en")
    yield
    set_lang(previous)


def _app(tmp_path, script, session=None, **overrides):
    """The real app, on the real loop, with a scripted endpoint."""
    kwargs = {"provider": PROVIDER, "model": MODEL, "native_tools": False,
              "mode": "normal", "permissions": {"mode": "auto"},
              "economy": {"cache_enabled": False,
                          "cache_dir": str(tmp_path / "cache")}}
    kwargs.update(overrides)
    app = BeeCodeApp(config=BeeConfig(**kwargs), session=session or Session())
    app.agent.workdir = str(tmp_path)
    app.agent.providers.register(Scripted(script), replace=True)
    return app


def log_lines(app):
    return [strip.text.rstrip() for strip in app.chatlog.lines]


def log_text(app) -> str:
    """Everything the chat log holds, in the order the user read it."""
    return "\n".join(log_lines(app))


def says(text: str, phrase: str) -> bool:
    """Is `phrase` on screen, ignoring where the log width broke the line?"""
    return "".join(phrase.split()) in "".join(text.split())


def status_line(app) -> str:
    """The status label as the user reads it."""
    from textual.widgets import Label

    return str(app.query_one("#status", Label).content)


def broken_call(tool: str, **args) -> str:
    """The reply shape the parser reads: one fenced json block."""
    return "```json\n" + json.dumps({"tool": tool, "args": args}) + "\n```"


async def _wait_until(pilot, predicate, timeout: float = 30.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await pilot.pause(0.05)
    return bool(predicate())


async def _settled(pilot, app, expect: str, timeout: float = 30.0) -> bool:
    """The turn is over when the agent is idle and the expected text is on screen.

    `agent.is_busy` alone cannot be watched: a scripted endpoint answers within
    one poll, so the flag goes True and False between two looks and the test
    concludes no run ever started.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        await pilot.pause(0.05)
        if not app.agent.is_busy and says(log_text(app), expect):
            return True
    return False


async def _drive(app, text: str = "what now?", expect: str = DONE):
    """Type one question into the mounted app and let the turn finish.

    Everything a test needs is read here, inside the mounted app: after
    `run_test` closes there is no screen left to ask for the log.
    """
    async with app.run_test(size=SIZE) as pilot:
        await pilot.pause(0.3)          # on_mount: theme, sidebar, welcome, status
        await pilot.click("#prompt")
        await pilot.press(*text)
        await pilot.press("enter")
        assert await _settled(pilot, app, expect), \
            f"the turn never landed {expect!r}:\n{log_text(app)[-1800:]}"
        await pilot.pause(0.3)
        return log_text(app), status_line(app), log_lines(app)


def driven(app, text: str = "what now?", expect: str = DONE):
    return asyncio.run(_drive(app, text, expect))


def worker_error(log: str) -> str:
    """A dead worker writes `agent error:` -- never acceptable in these tests."""
    return next((line for line in log.splitlines() if line.startswith("agent error:")), "")


def agent_source() -> str:
    """`core/agent.py` as text: the list of events the loop can emit is read
    from it, so this file cannot drift behind a loop that learned a new event."""
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    return (root / "beeagent" / "core" / "agent.py").read_text(encoding="utf-8")


# --- the eight events, each driven through the real loop -------------------

def test_stream_reset_says_the_fragment_was_thrown_away(tmp_path):
    """The proven one: the screen held "half an answer… the real answer"."""
    app = _app(tmp_path, [{"die_after": PARTIAL}])
    log, status, lines = driven(app, expect=CLEAN)
    assert not worker_error(log)

    assert says(log, "the unfinished answer was thrown away, not appended"), log[-1800:]
    assert says(log, "Gone: " + str(len(PARTIAL)) + " characters"), "how much was dropped"
    assert says(log, PARTIAL), "and which text it was"
    # The fragment survives only inside the note that mourns it: it is named
    # once, and never on the same line as the clean answer.
    assert log.count("PARTIAL") == 1, log[-1800:]
    assert not [line for line in lines if "PARTIAL" in line and CLEAN in line]
    assert says(log, CLEAN), "the retry's answer still reached the screen"
    assert says(status, "this turn") and says(status, "thrown away"), status


def test_context_trimmed_names_what_fell_out_and_how_much(tmp_path):
    """A trimmed history may not be announced as "the task stays in view"."""
    session = Session()
    for index in range(12):
        session.messages.append(Message("user", f"question {index} " + "lorem ipsum dolor " * 120))
        session.messages.append(Message("assistant",
                                        f"answer {index} " + "consectetur elit " * 120))
    app = _app(tmp_path, [DONE], session=session, max_context_tokens=1024)
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "history did not fit the window"), log[-1800:]
    assert says(log, "message(s)"), "how many"
    assert says(log, "characters"), "how much text"
    assert says(log, "user x") and says(log, "assistant x"), "which ones, by role"
    assert says(log, "a digest line instead of them"), "what the model gets now"
    assert says(log, "/history keeps the full text"), "where the real text is"
    assert "stays in view" not in log, "the old note promised exactly what it clipped"
    assert says(status, "summarised"), status
    assert says(log, "this turn:"), "the tally is in the log, not only the label"
    assert says(log, "msg(s) summarised away"), log[-800:]

    # A turn with nothing trimmed must not claim there was anything to report.
    plain = _app(tmp_path, [DONE])
    plain_log, plain_status, _plain_lines = driven(plain)
    assert not says(plain_log, "did not fit")
    assert "this turn" not in plain_status, plain_status


def test_waiting_says_the_endpoint_is_only_slow(tmp_path, monkeypatch):
    """Silence from a free endpoint looked like a dead program."""
    import beeagent.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "HEARTBEAT_SECONDS", 0.3)
    app = _app(tmp_path, [{"stall": 0.7, "chunks": [("content", DONE)]}],
               stream_idle_timeout=9)
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "the endpoint has said nothing for 0.3s"), log[-1800:]
    assert says(log, "still waiting, nothing was lost")
    assert says(status, "this turn") and says(status, "wait"), status
    assert says(log, DONE), "and the answer came when the endpoint did"


def test_tool_repaired_shows_what_was_sent_versus_what_ran(tmp_path):
    (tmp_path / "note.txt").write_text("pelmeni", encoding="utf-8")
    # Short by two braces: the parser closes it and runs it, and has to say so.
    broken = 'One moment.\n```json\n{"tool": "read", "args": {"path": "note.txt"'
    app = _app(tmp_path, [broken, DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "incomplete tool call repaired before running"), log[-1800:]
    assert says(log, "added the missing"), "what was wrong with it"
    assert says(log, 'The model sent \u201c{"tool": "read"'), "its own bytes, quoted"
    assert says(log, "the line that runs next is the fixed shape")
    assert RUN_MARK + " read" in log, "and the repaired call really ran"
    assert says(status, "repaired"), status


def test_tool_dropped_says_that_nothing_ran(tmp_path):
    """A cut-off call is the failure a user reads as being ignored."""
    cut = ('Writing it now.\n```json\n{"tool": "write", "args": {"path": "note.txt", '
           '"content": "first line\nsecond line')
    app = _app(tmp_path, [cut, DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "a tool call arrived cut off and NOTHING ran"), log[-1800:]
    assert says(log, 'The model sent \u201c{"tool": "write"'), "what it actually sent"
    assert says(log, "asked to send it again")
    assert RUN_MARK + " write" not in log, "the note says nothing ran, and nothing did"
    assert says(log, DONE), "the retry's answer is still there"
    assert says(status, "NOT run"), status


def test_tool_renamed_says_the_name_the_model_used(tmp_path):
    app = _app(tmp_path, [broken_call("read_directory", path="."), DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "the model called it \u201cread_directory\u201d,"
                     " which is not the tool's name"), log[-1800:]
    assert says(log, "runs as \u201clist_directory\u201d"), "the name that ran"
    assert says(log, "written to history under that name")
    assert RUN_MARK + " list_directory" in log, "it ran under the corrected name"
    assert says(status, "corrected"), status


def test_tool_unknown_says_which_name_was_never_a_tool(tmp_path):
    app = _app(tmp_path, [broken_call("quantum_flux_capacitor"), DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "\u201cquantum_flux_capacitor\u201d is not a tool here"
                     " \u2014 nothing ran"), log[-1800:]
    assert says(log, "the model got the real list back")
    assert RUN_MARK + " quantum_flux_capacitor" not in log
    assert says(status, "unknown tool"), status


def test_nudged_says_the_turn_was_not_an_answer(tmp_path):
    """"Now I will read the file" is a half-finished turn, not a reply."""
    promise = "Now I will read the file and tell you what is in it."
    app = _app(tmp_path, [promise, DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)

    assert says(log, "the model promised a step but sent no tool call"), log[-1800:]
    assert says(log, "nothing ran")
    assert "read the file" in log, "the promise itself is quoted back"
    assert says(log, "the next answer is the same question re-asked")
    assert says(log, DONE), "and the answer that followed still arrived"
    assert says(status, "nudge"), status
    # The turn closes with the same tally the status line carries, so the record
    # survives even when the label has scrolled out of sight.
    assert says(log, "this turn: 1 nudge(s) to act"), log[-800:]


# --- the pairing, and the guard behind it ----------------------------------

# The eight, with the payloads the loop really sends, and the half of the note
# that says what happened.
ALL_EVENTS = {
    "stream_reset": ({}, "thrown away, not appended"),
    "context_trimmed": ({"dropped": 2}, "history did not fit the window"),
    "waiting": ({"seconds": 15}, "the endpoint has said nothing for 15s"),
    "tool_dropped": ({"notes": ['the "read" call was cut off inside "path"']},
                     "NOTHING ran"),
    "tool_repaired": ({"notes": ["added the missing }}"]}, "repaired before running"),
    "tool_renamed": ({"from": "reed", "to": "read"}, "which is not the tool's name"),
    "tool_unknown": ({"tool": "quantum_flux_capacitor"}, "is not a tool here"),
    "nudged": ({}, "promised a step but sent no tool call"),
}


def _one_event(tmp_path, event, language):
    """Fire exactly one loop event into the mounted app; return the log and label."""
    app = _app(tmp_path, [DONE], session=_three_message_session())

    async def scenario():
        set_lang(language)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            app._stream_buf = PARTIAL
            app._sent_buf = '{"tool": "read", "args": {"path": "note.txt"'
            app._on_agent_event(event, ALL_EVENTS[event][0])
            await pilot.pause(0.2)
            return log_text(app)

    try:
        return asyncio.run(scenario())
    finally:
        set_lang("en")


def _three_message_session() -> Session:
    session = Session()
    session.messages.append(Message("user", "the pelmeni recipe again"))
    session.messages.append(Message("assistant", "flour eggs water and patience"))
    session.messages.append(Message("user", "and the sauce"))
    return session


@pytest.mark.parametrize("event", sorted(ALL_EVENTS))
def test_each_event_writes_its_own_note_in_both_languages(tmp_path, event):
    """One event, one note, in either language -- and only that note.

    A note that exists in English only would print the English line under
    `set_lang("ru")`, which is what the second half of this test refuses.
    """
    marker = ALL_EVENTS[event][1]

    english = _one_event(tmp_path, event, "en")
    assert says(english, marker), f"{event} wrote no note at all:\n{english[-1200:]}"
    for other, (_payload, other_marker) in ALL_EVENTS.items():
        if other != event:
            assert not says(english, other_marker), f"{other}'s note leaked into {event}"
    assert not CYRILLIC.search(english)

    russian = _one_event(tmp_path, event, "ru")
    assert not says(russian, marker), \
        f"{event} printed the English note while the language was Russian"
    noted = [line for line in russian.splitlines() if line.startswith("  ")]
    assert noted, f"{event} wrote nothing in Russian"
    assert CYRILLIC.search("\n".join(noted)), f"{event}: no Russian in {noted}"
    # The data a note carries is the same in either language.
    if event in ("tool_renamed", "tool_unknown"):
        assert "reed" in russian or "quantum_flux_capacitor" in russian
    if event in ("tool_repaired", "tool_dropped", "nudged", "stream_reset"):
        assert "note.txt" in russian or "PARTIAL" in russian, \
            f"{event}: the Russian note dropped the data the English one has"


def test_the_interface_has_a_branch_for_every_loop_event(tmp_path):
    """All 24 events `Agent.run` emits, fired one by one at the mounted app.

    A branch that raises is as invisible as a branch that is missing: the
    callback swallows it, the user sees nothing and the turn carries on. So
    every event name from `callback("...")` in core/agent.py gets its payload
    here, and each one that means something to a human has to leave a line in
    the log.
    """
    payloads = {
        "status": {}, "stream_delta": {"text": "some tokens"},
        "reasoning_delta": {"text": "some thinking"},
        "stream_reset": {}, "retry": {"attempt": 2}, "waiting": {"seconds": 15},
        "context_trimmed": {"dropped": 1}, "economy_hit": {},
        "response": {"text": "cached answer"}, "done": {"text": DONE},
        "error": {"message": "the endpoint refused"},
        "stopped": {"turn": 1}, "queued_sent": {"items": ["typed while busy"]},
        "model_switched": {"from": "gpt-4", "to": "command-a-03-2025"},
        "provider_fallback": {"from": "g4f", "to": "pool", "seat": True},
        "tool_start": {"tool": "read", "args": {"path": "note.txt"}},
        "tool_end": {"tool": "read", "args": {}, "output": "1: pelmeni", "error": False},
        "tool_error": {"tool": "bash", "message": "missing argument(s): command"},
        "tool_denied": {"tool": "write", "args": {}, "message": "not allowed"},
        "tool_repaired": {"notes": ["added the missing }}"]},
        "tool_dropped": {"notes": ['the "write" call was cut off inside "content"']},
        "tool_renamed": {"from": "reed", "to": "read"},
        "tool_unknown": {"tool": "quantum_flux_capacitor"},
        "nudged": {},
    }
    # Text that only moves the live stream line, never the log.
    stream_only = {"status", "stream_delta", "reasoning_delta"}
    names = set(re.findall(r'callback\("([a-z_]+)"', agent_source()))
    assert names == set(payloads), f"the loop emits {sorted(names - set(payloads))} " \
                                   f"this list does not know; " \
                                   f"{sorted(set(payloads) - names)} are stale"

    app = _app(tmp_path, [DONE], session=_three_message_session())

    async def scenario():
        silent = []
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            for event, payload in payloads.items():
                app._stream_buf = "some tokens"
                app._sent_buf = '{"tool": "read", "args": {"path": "note.txt"'
                before = len(app.chatlog.lines)
                app._on_agent_event(event, payload)
                await pilot.pause(0.05)
                if event not in stream_only and len(app.chatlog.lines) == before:
                    silent.append(event)
        return silent

    silent = asyncio.run(scenario())
    assert not silent, f"these events reached the interface and said nothing: {silent}"


def test_an_event_no_branch_handles_is_still_reported(tmp_path):
    """A future loop event must not be able to arrive silently again."""
    app = _app(tmp_path, [DONE])

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            app._on_agent_event("brand_new_signal", {"why": "because"})
            app._on_agent_event("brand_new_signal", {"why": "again"})
            await pilot.pause(0.2)
            return log_lines(app)

    lines = asyncio.run(scenario())
    reported = [line for line in lines if "brand_new_signal" in line]
    assert len(reported) == 1, f"an unknown event is said once, not spammed: {lines}"
    assert says("\n".join(lines), "unhandled agent event")


# --- today's work, still standing ------------------------------------------

def test_the_reasoning_display_still_ships(tmp_path):
    """A `reasoning_delta` branch and the thinking dump into the chat log."""
    app = _app(tmp_path, [{"chunks": [("reasoning", "counting the pelmeni "),
                                      ("reasoning", "one two three"),
                                      ("content", DONE)]}])
    log, _status, _lines = driven(app)
    assert not worker_error(log)
    assert says(log, "counting the pelmeni one two three"), log[-1800:]
    assert says(log, "what I thought"), "the block says whose thinking it is"
    assert says(log, DONE), "and the answer came after it"


def test_stop_still_interrupts_the_running_answer(tmp_path):
    """/stop still reaches the loop, the worker and the log."""
    app = _app(tmp_path, [DONE])
    state = {"hold": True}

    async def fake_stream(provider, messages, callback=None, model=""):
        if callback:
            callback("stream_delta", {"text": PARTIAL})
        while state["hold"] and not app.agent.stop_requested:
            await asyncio.sleep(0.02)
        return broken_call("read", path="note.txt")

    app.agent._stream_response = fake_stream

    async def scenario():
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.3)
            await pilot.click("#prompt")
            await pilot.press(*"start the long job")
            await pilot.press("enter")
            assert await _wait_until(pilot, lambda: app.agent.is_busy), "no run started"
            await pilot.click("#prompt")
            await pilot.press(*"/stop")
            await pilot.press("enter")
            assert await _wait_until(pilot, lambda: not app.agent.is_busy, 15.0), \
                "the busy flag is stuck and the prompt is locked"
            state["hold"] = False
            await pilot.pause(0.3)
            return log_text(app)

    log = asyncio.run(scenario())
    assert "/stop" in log, "what the user typed is shown back to them"
    assert says(log, "stopping after this step"), log[-1500:]
    assert STOP_MARK in log, "the stopped note never reached the log"
    assert says(log, "/tasks"), "and it says the session is intact"


def test_a_clean_turn_claims_nothing(tmp_path):
    """The status line may not invent incidents the turn never had."""
    app = _app(tmp_path, [DONE])
    log, status, lines = driven(app)
    assert not worker_error(log)
    assert says(log, DONE)
    assert "this turn" not in status, status
    assert not [line for line in lines if "this turn" in line]
