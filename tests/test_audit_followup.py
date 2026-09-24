"""Regressions for the audit findings that survived verification.

Every test here was measured against the code first: each one fails on the
behaviour the file had before the fix, and passes on the behaviour it has now.
The claims the audit made that the code does not actually exhibit are listed in
the commit notes, not here — a test for a bug that is not there locks in a
behaviour nobody wants.
"""
import asyncio
import json
import os
import sys

import pytest

from beeagent.config.loader import load_config, save_config
from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session


class NativeMute:
    """A tool-calling endpoint that answers with neither text nor a call."""

    name = "nativemute"
    supports_tools = True
    models = []

    def __init__(self, answer=None):
        self.answer = {"text": "", "tool_calls": []} if answer is None else answer
        self.answers = None
        self.calls = 0

    async def complete(self, messages, model, tool_schemas):
        self.calls += 1
        if self.answers:
            return self.answers.pop(0)
        return self.answer

    async def chat(self, messages, model=""):
        return ""


class Speaker:
    """A plain endpoint that always has something to say."""

    name = "speaker"
    supports_tools = False
    models = []

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, model=""):
        self.calls += 1
        return "done talking"


def _agent(tmp_path, provider, **config_kwargs):
    config = BeeConfig(provider=provider.name, native_tools=provider.supports_tools,
                       **config_kwargs)
    agent = Agent(config=config, workdir=str(tmp_path))
    agent.providers.register(provider, replace=True)
    agent.config.provider = provider.name
    return agent


def _run(agent, question="сделай файл"):
    events, session = [], Session()
    answer = asyncio.run(agent.run(question, session=session,
                                   callback=lambda kind, payload: events.append(kind)))
    return answer, events, session


# --- an error path that reported success ------------------------------------

def test_a_native_endpoint_that_said_nothing_fails_loudly(tmp_path):
    """Empty is a failure on the streamed path and used to be an answer here."""
    agent = _agent(tmp_path, NativeMute())
    answer, events, session = _run(agent)

    assert "empty answer" in answer, f"the failure was dressed up as a reply: {answer!r}"
    assert "error" in events and "done" not in events, events
    assert [m.to_dict() for m in session.messages if m.role == "assistant"] == [], \
        "an answer that was never said does not belong in the transcript"


def test_a_native_turn_that_carries_a_tool_call_is_still_an_answer(tmp_path):
    """The guard must not swallow a silent turn that did send a call."""
    provider = NativeMute()
    provider.answers = [
        {"text": "", "tool_calls": [{"tool": "read", "args": {"path": "a.txt"}}]},
        {"text": "the file is not there", "tool_calls": []},
    ]
    agent = _agent(tmp_path, provider)
    answer, events, session = _run(agent)

    assert "empty answer" not in answer
    assert "Error calling provider" not in answer
    assert provider.calls == 2, events
    assert any(kind in ("tool_start", "tool_end", "tool_denied", "tool_unknown")
               for kind in events), events


def test_max_turns_of_zero_still_asks_the_model(tmp_path):
    """`max_turns: 0` ended the run before the first request and lied about it."""
    speaker = Speaker()
    agent = _agent(tmp_path, speaker, max_turns=0)
    answer, events, session = _run(agent, "hi")

    assert speaker.calls == 1, "the endpoint was never asked anything"
    assert answer == "done talking", answer
    assert "error" not in events, events


# --- config state that did not survive a restart ----------------------------

def test_a_second_unreadable_config_keeps_the_first_rescue(tmp_path):
    """Two bad configs in a row used to leave one set of keys on disk, not two."""
    first = tmp_path / "beeagent.json"
    first.write_text('{"api_keys": "FIRST_MARKER", "model": "gpt-4o"}', encoding="utf-8")
    load_config(str(tmp_path))
    assert "FIRST_MARKER" in (tmp_path / "beeagent.json.broken").read_text(encoding="utf-8")

    (tmp_path / "beeagent.json").write_text('{"permissions": "not-a-dict"}', encoding="utf-8")
    load_config(str(tmp_path))

    rescued = "".join(p.read_text(encoding="utf-8")
                      for p in tmp_path.glob("beeagent.json.broken*"))
    assert "FIRST_MARKER" in rescued, "the first rescue was deleted by the second"
    assert "not-a-dict" in rescued, "the second bad config was not kept either"


def test_a_config_that_cannot_be_moved_is_copied_aside(tmp_path, monkeypatch):
    """Windows refuses the rename while the file is open in an editor.

    The keys survived only as long as `replace` worked; when it did not,
    BeeCode ran on defaults and the next save wrote straight over them.
    """
    (tmp_path / "beeagent.json").write_text('{"api_keys": "MARKER_STRING"}', encoding="utf-8")
    real_replace = os.replace

    def refused(src, dst, *args, **kwargs):
        if str(src).endswith("beeagent.json"):
            raise OSError("the file is open in another program")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr("beeagent.config.loader.os.replace", refused)
    config = load_config(str(tmp_path))
    monkeypatch.undo()

    assert config.api_keys == {}, "BeeCode still starts on defaults"
    save_config(config, str(tmp_path))
    on_disk = "".join(p.read_text(encoding="utf-8") for p in tmp_path.glob("beeagent.json*"))
    assert "MARKER_STRING" in on_disk, "the stored keys were overwritten with defaults"


# --- session state that did not survive a crash -----------------------------

def test_a_failed_session_save_leaves_no_stray_temp(tmp_path, monkeypatch):
    """The old fixed `<id>.json.tmp` stayed on disk after a failed rename."""
    from beeagent.core import session as session_module

    sessions = tmp_path / ".beeagent" / "sessions"
    sessions.mkdir(parents=True)
    record = Session("fixed_id")
    record.add_user_message("one")
    record.save(str(tmp_path))

    def refused(src, dst, *args, **kwargs):
        raise OSError("cross-device link")

    monkeypatch.setattr(session_module.os, "replace", refused)
    with pytest.raises(OSError):
        record.save(str(tmp_path))

    assert sorted(p.name for p in sessions.iterdir()) == ["fixed_id.json"], \
        "a half-written temp outlived the save that failed"
    assert json.loads((sessions / "fixed_id.json").read_text(encoding="utf-8"))["messages"]


def test_two_saves_of_one_session_never_share_a_temp_name(tmp_path, monkeypatch):
    """Two windows resumed on the same id wrote the same temp file."""
    from beeagent.core import session as session_module

    seen = []
    real_replace = os.replace

    def spy(src, dst, *args, **kwargs):
        seen.append(str(src))
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(session_module.os, "replace", spy)
    for text in ("first window", "second window"):
        record = Session("shared_id")
        record.add_user_message(text)
        record.save(str(tmp_path))

    assert len(seen) == 2 and seen[0] != seen[1], f"both saves used {seen}"
    assert Session.load("shared_id", str(tmp_path)).messages[-1].content == "second window"


# --- three findings the audit listed and nobody owned -----------------------

def test_a_cache_hit_shows_its_answer_in_the_tui(tmp_path):
    """An economy hit fires `response` and returns; the TUI had no branch for it.

    The answer existed in the session and the user watched the status line clear
    on an empty screen — the second identical question looked like a hang.
    """
    from beeagent.ui.tui import BeeCodeApp

    config = BeeConfig(provider="speaker", mode="economy", native_tools=False,
                       economy={"cache_enabled": True,
                                "cache_dir": str(tmp_path / "cache"),
                                "cache_ttl_minutes": 60})
    app = BeeCodeApp(config=config, session=Session())
    app.agent.providers.register(Speaker(), replace=True)
    app.agent.workdir = str(tmp_path)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            from textual.widgets import RichLog

            for _ in range(2):
                await pilot.click("#prompt")
                await pilot.press(*"сколько будет дважды два")
                await pilot.press("enter")
                for _ in range(120):
                    await pilot.pause(0.1)
                    if not app.agent.is_busy:
                        break
            lines = [getattr(i, "plain", "") or str(getattr(i, "renderable", i))
                     for i in list(app.query_one("#log", RichLog).lines)]
            return "\n".join(lines)

    log = asyncio.run(scenario())
    assert log.count("done talking") == 2, (
        "the second answer was served from the cache and never shown:\n" + log[-1500:])


def test_cut_bash_output_says_it_was_cut(tmp_path, monkeypatch):
    """`truncate_note` existed and had no caller: a 5 MB log ended mid-word and
    read to the model, and to the user, as if the program had stopped there."""
    from beeagent.tools import shell

    monkeypatch.chdir(tmp_path)
    big = shell.MAX_CAPTURE + 10
    out, err, code = shell.run_argv_text(
        [sys.executable, "-c", "print('x' * %d)" % big])
    assert len(out) > shell.MAX_CAPTURE, "the head of the output must still be there"
    assert "output cut at" in out, out[-200:]


def test_a_plugin_token_never_reaches_config_output():
    """`/config` output ends up in screenshots; `extensions` is plugin-owned and
    was the one dict the redactor did not look inside."""
    from beeagent.ui.commands import _redact_secrets

    config = BeeConfig(extensions={"recall": {"api_token": "supersecret_1234",
                                              "path": "./notes"}})
    shown = str(_redact_secrets(config.model_dump()))
    assert "supersecret_1234" not in shown
    assert "1234" in shown and "./notes" in shown
