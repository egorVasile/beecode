"""Asking the human when the model leaves the working directory.

A refusal for an outside path is a question (decline / once / always), not a
dead end. A non-terminal never asks. One test per answer, plus the full loop.
"""
import asyncio
import sys

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core import executor
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.tools import _path_policy as policy


@pytest.fixture()
def tty(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


@pytest.fixture()
def isolated_roots(tmp_path, monkeypatch):
    import tempfile

    monkeypatch.chdir(tmp_path)
    # The real temp dir would allow everything under it: point the root at
    # the test folder itself, so a sibling folder is genuinely outside.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    policy.forget_granted_roots()
    if policy.always():
        # Another test answered "always": the flag is process-global.
        policy._always = False
    yield
    policy.forget_granted_roots()
    policy._always = False


def test_decline_keeps_the_refusal(tmp_path, tty, isolated_roots, monkeypatch):
    from beeagent.tools.read import ReadTool

    outside = str(tmp_path.parent / "elsewhere")
    assert ReadTool().execute(path=outside).error is True
    monkeypatch.setattr("builtins.input", lambda *a: "1")
    assert asyncio.run(executor._maybe_grant_outside(None, "read")) is False
    assert policy._granted == []
    assert ReadTool().execute(path=outside).error is True


def test_once_grants_this_folder(tmp_path, tty, isolated_roots, monkeypatch):
    from beeagent.tools.read import ReadTool

    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "note.txt").write_text("hi", encoding="utf-8")
    assert ReadTool().execute(path=str(outside)).error is True
    monkeypatch.setattr("builtins.input", lambda *a: "2")
    assert asyncio.run(executor._maybe_grant_outside(None, "read")) is True
    assert ReadTool().execute(path=str(outside / "note.txt")).error is False


def test_always_stops_asking(tmp_path, tty, isolated_roots, monkeypatch):
    from beeagent.tools.read import ReadTool

    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    assert ReadTool().execute(path=str(outside)).error is True
    monkeypatch.setattr("builtins.input", lambda *a: "3")
    assert asyncio.run(executor._maybe_grant_outside(None, "read")) is True
    assert policy.always() is True
    # A second, different outside folder: no question asked.
    other = tmp_path.parent / "another"
    other.mkdir(exist_ok=True)
    (other / "n.txt").write_text("x", encoding="utf-8")
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "1")
    assert ReadTool().execute(path=str(other / "n.txt")).error is False
    assert asked == [], "always means no more prompts"


def test_no_terminal_never_asks(tmp_path, isolated_roots, monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    from beeagent.tools.read import ReadTool

    outside = tmp_path.parent / "elsewhere"
    asked = []
    monkeypatch.setattr("builtins.input", lambda *a: asked.append(1) or "2")
    assert ReadTool().execute(path=str(outside)).error is True
    assert asyncio.run(executor._maybe_grant_outside(None, "read")) is False
    assert asked == []


def test_loop_reruns_after_once(tmp_path, tty, isolated_roots, monkeypatch):
    """End to end: refused read, "2", the tool runs, the model sees output."""
    from beeagent.tools.read import ReadTool

    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    (outside / "note.txt").write_text("сорок два", encoding="utf-8")
    monkeypatch.setattr("builtins.input", lambda *a: "2")

    class AskThenRead:
        name = "ask-then-read"
        models = ["m"]
        supports_tools = False

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            if self.calls == 1:
                yield ("content", '{"tool": "read", "args": {"path": "%s"}}'
                       % str(outside / "note.txt").replace("\\", "\\\\"))
            else:
                yield ("content", "прочитано")

        async def chat(self, messages, model=""):
            return "прочитано"

    agent = Agent(config=BeeConfig(permissions={"mode": "auto"}), workdir=str(tmp_path))
    endpoint = AskThenRead()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint

    answer = asyncio.run(agent.run("прочитай", session=Session()))
    assert "прочитано" in answer
    assert endpoint.calls == 2
