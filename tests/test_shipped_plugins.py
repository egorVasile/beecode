"""The plugins we ship in the catalog must work when a user installs them.

Each test copies the real template into a project, loads it the way the loader
does, and drives the command through the same dispatch the terminal uses.
"""
import json
import shutil
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.ext.api import emit
from beeagent.ui.commands import ReplContext, dispatch

TEMPLATES = Path(__file__).resolve().parent.parent / "beeagent" / "plugins" / "templates" / "plugins"
SHIPPED = ["undo", "doctor", "changes", "recall"]


@pytest.fixture()
def project(tmp_path, monkeypatch):
    """A working directory of its own — the loader reads .beeagent from the cwd."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


def load(project, name):
    shutil.copytree(TEMPLATES / name, project / ".beeagent" / "plugins" / name,
                    dirs_exist_ok=True)
    state = project / ".beeagent" / "plugins.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"installed": {name: {"type": "plugin", "enabled": True}}}),
                     encoding="utf-8")
    agent = Agent(config=BeeConfig(), workdir=str(project))
    return agent


def run(agent, line):
    return dispatch(ReplContext(agent=agent, config=agent.config, session=Session()), line)


def plain(result):
    body = result.output
    if isinstance(body, str):
        return body
    from rich.console import Console
    import io

    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    console.print(body)
    return console.file.getvalue()


def test_every_shipped_plugin_loads_without_errors(project):
    for name in SHIPPED:
        agent = load(project, name)
        assert not [e for e in agent.plugins.load_errors], (name, agent.plugins.load_errors)


def test_undo_saves_a_copy_before_the_write_and_gives_it_back(project):
    agent = load(project, "undo")
    target = project / "recipe.txt"
    target.write_text("как было", encoding="utf-8")

    emit(agent.plugins.extensions, "tool_start",
         {"tool": "write", "args": {"path": "recipe.txt"}})
    target.write_text("как стало, и это не то что надо", encoding="utf-8")

    result = run(agent, "/undo")
    assert "restored" in plain(result)
    assert target.read_text(encoding="utf-8") == "как было"
    # What the agent wrote is not thrown away — undoing an undo stays possible.
    assert any("discarded" in p.name for p in (project / ".beeagent" / "undo").glob("*/discarded-*"))


def test_undo_does_not_snapshot_what_a_tool_did_not_touch(project):
    agent = load(project, "undo")
    (project / "quiet.txt").write_text("тихо", encoding="utf-8")
    emit(agent.plugins.extensions, "tool_start", {"tool": "bash", "args": {"command": "ls"}})
    assert "nothing to undo" in plain(run(agent, "/undo"))


def test_undo_leaves_files_outside_the_project_alone(project):
    agent = load(project, "undo")
    elsewhere = project.parent / "not-mine.txt"
    elsewhere.write_text("не трогать", encoding="utf-8")
    emit(agent.plugins.extensions, "tool_start",
         {"tool": "write", "args": {"path": str(elsewhere)}})
    assert not (project / ".beeagent" / "undo").exists()


def test_doctor_reports_the_install_without_touching_the_network(project, monkeypatch):
    agent = load(project, "doctor")

    def forbid(*args, **kwargs):
        raise AssertionError("/doctor must not open a connection")

    monkeypatch.setattr("socket.socket", forbid)
    text = plain(run(agent, "/doctor"))
    for line in ("python", "g4f", "model window", "extensions"):
        assert line in text, text


def test_doctor_names_the_android_commands_that_make_the_rest_work(project, monkeypatch):
    """On Termux the usual fixes are wrong: there is no apt, no compiler, and the
    SD card is invisible until the user mounts it. The check-up has to say the
    `pkg` and `termux-…` lines, or it tells a phone user to do impossible things."""
    agent = load(project, "doctor")
    monkeypatch.setenv("PREFIX", "/data/data/com.termux/files/usr")
    monkeypatch.setenv("HOME", "/data/data/com.termux/files/home")

    text = plain(run(agent, "/doctor"))
    for label in ("termux prefix", "bash for the shell tool", "shared storage"):
        assert label in text, f"{label} missing from:\n{text}"
    assert "termux-setup-storage" in text, "the SD card is invisible until it is mounted"

    # A desktop must not be lectured about pkg.
    monkeypatch.delenv("PREFIX", raising=False)
    monkeypatch.setenv("HOME", str(project))
    assert "termux prefix" not in plain(run(agent, "/doctor"))


def test_changes_lists_what_this_session_wrote(project):
    agent = load(project, "changes")
    (project / "app.py").write_text("print(1)", encoding="utf-8")
    emit(agent.plugins.extensions, "tool_end",
         {"tool": "write", "args": {"path": "app.py"}, "output": "ok", "error": False})
    text = plain(run(agent, "/changes"))
    assert "app.py" in text


def test_recall_searches_saved_sessions_and_names_the_one_to_reopen(project):
    agent = load(project, "recall")
    sessions = project / ".beeagent" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "20260101_000000_ab12cd.json").write_text(json.dumps({
        "id": "20260101_000000_ab12cd",
        "created_at": "2026-01-01T00:00:00",
        "messages": [{"role": "user", "content": "помни: парсер чиним через state machine"},
                     {"role": "assistant", "content": "ок"}],
    }, ensure_ascii=False), encoding="utf-8")

    text = plain(run(agent, "/recall state machine"))
    assert "20260101_000000_ab12cd" in text
    assert "парсер" in text
    assert "nothing in" not in text


def test_a_second_agent_in_the_same_process_keeps_the_commands(project):
    """Registering an extension command again replaces it — it is not a collision."""
    load(project, "undo")
    second = load(project, "undo")
    assert not [c for c in second.plugins.extensions.contributions if "refused" in c.note]
    assert "nothing to undo" in plain(run(second, "/undo"))
