"""Commands ported from Claude Code / Codex: /init, /memory, /context, /fork,
/goal, /doctor, /dirs, plus the hidden /usage and /resume aliases.

One test per behaviour that matters: scaffold-then-refuse, set-then-show,
clone-then-walk-back, grant-then-list-then-clear. The registry assertions pin
the port to the shared machinery (dispatch + /help + README sync read the same
COMMANDS list).
"""
import io

import pytest
from rich.console import Console

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.ui.commands import COMMANDS, HANDLERS, ReplContext, dispatch


class FakeAgent:
    """Workdir only — what the file-scoped commands read."""

    def __init__(self, workdir):
        self.workdir = str(workdir)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture()
def ctx(project):
    return ReplContext(agent=FakeAgent(project), config=BeeConfig(), session=Session())


@pytest.fixture()
def live_ctx():
    cfg = BeeConfig()
    return ReplContext(agent=Agent(config=cfg), config=cfg, session=Session())


def text(result) -> str:
    body = result.output
    if isinstance(body, str):
        return body
    buffer = io.StringIO()
    Console(file=buffer, width=200).print(body)
    return buffer.getvalue()


# --- the port is registered where /help and the README look -----------------

PORTED = ["init", "memory", "context", "fork", "goal", "doctor", "dirs"]


def test_ported_commands_registered():
    names = {c.name for c in COMMANDS}
    for name in PORTED:
        assert name in names, f"/{name} missing from COMMANDS (/help, sidebar, README)"
        assert name in HANDLERS, f"/{name} missing from HANDLERS (dispatch)"


def test_hidden_aliases_dispatch():
    assert "usage" in HANDLERS and "resume" in HANDLERS
    assert HANDLERS["usage"] is HANDLERS["stats"]
    assert HANDLERS["resume"] is HANDLERS["continue"]


# --- /init + /memory ----------------------------------------------------------

def test_init_scaffolds_then_refuses(ctx, project):
    first = text(dispatch(ctx, "/init"))
    assert (project / "AGENTS.md").is_file()
    assert "AGENTS.md" in first
    second = text(dispatch(ctx, "/init")).lower()
    assert "already exists" in second or "уже есть" in second
    # The refusal kept the bytes: no second write clobbered the scaffold.
    assert (project / "AGENTS.md").read_text(encoding="utf-8").startswith("# ")


def test_memory_lists_and_shows(ctx, project):
    (project / "AGENTS.md").write_text("# notes\nremember the tests\n", encoding="utf-8")
    listing = text(dispatch(ctx, "/memory"))
    assert "AGENTS.md" in listing
    shown = text(dispatch(ctx, "/memory agents"))
    assert "remember the tests" in shown
    missing = text(dispatch(ctx, "/memory no-such-file")).lower()
    assert "no memory file" in missing or "нет" in missing


# --- /goal -------------------------------------------------------------------

def test_goal_set_show_pause_resume_done_clear(ctx, project):
    assert "no goal" in text(dispatch(ctx, "/goal")).lower() \
        or "не задана" in text(dispatch(ctx, "/goal"))
    assert "ship it" in text(dispatch(ctx, "/goal ship it"))
    assert (project / ".beeagent" / "goal.json").is_file()
    assert "ship it" in text(dispatch(ctx, "/goal"))
    assert "paus" in text(dispatch(ctx, "/goal pause")).lower() \
        or "пауз" in text(dispatch(ctx, "/goal pause"))
    assert "paused" in text(dispatch(ctx, "/goal")).lower() \
        or "паузе" in text(dispatch(ctx, "/goal"))
    assert "active" in text(dispatch(ctx, "/goal resume")).lower() \
        or "активна" in text(dispatch(ctx, "/goal resume"))
    assert "done" in text(dispatch(ctx, "/goal done")).lower() \
        or "выполнена" in text(dispatch(ctx, "/goal done"))
    dispatch(ctx, "/goal clear")
    assert not (project / ".beeagent" / "goal.json").exists()


# --- /fork -------------------------------------------------------------------

def test_fork_clones_and_keeps_parent(ctx):
    ctx.session.add_user_message("hello")
    ctx.session.add_assistant_message("hi")
    parent_id = ctx.session.session_id
    out = text(dispatch(ctx, "/fork"))
    child_id = ctx.session.session_id
    assert parent_id != child_id
    assert parent_id in out and child_id in out
    assert len(ctx.session.messages) == 2
    # The parent on disk still holds both turns.
    loaded = Session.load(parent_id, ".")
    assert len(loaded.messages) == 2


def test_fork_empty_session_says_so(ctx):
    out = text(dispatch(ctx, "/fork")).lower()
    assert "nothing to fork" in out or "нечего" in out


# --- /dirs -------------------------------------------------------------------

def test_dirs_add_list_clear(ctx, tmp_path, monkeypatch):
    from beeagent.tools import _path_policy as policy

    policy.forget_granted_roots()
    policy._always = False
    try:
        outside = tmp_path.parent / "granted-dir"
        outside.mkdir(exist_ok=True)
        assert "granted" in text(dispatch(ctx, f"/dirs add {outside}")).lower() \
            or "разрешено" in text(dispatch(ctx, f"/dirs add {outside}"))
        assert "granted-dir" in text(dispatch(ctx, "/dirs"))
        dispatch(ctx, "/dirs clear")
        assert "granted-dir" not in text(dispatch(ctx, "/dirs"))
    finally:
        policy.forget_granted_roots()
        policy._always = False


def test_dirs_add_needs_a_path(ctx):
    out = text(dispatch(ctx, "/dirs add")).lower()
    assert "usage" in out or "использование" in out


# --- /doctor -----------------------------------------------------------------

def test_doctor_reports_python(live_ctx):
    out = text(dispatch(live_ctx, "/doctor")).lower()
    for needle in ("python", "provider", "sessions"):
        assert needle in out, f"/doctor never mentioned {needle!r}"


# --- /context ----------------------------------------------------------------

def test_context_shows_tokens_and_ceiling(live_ctx):
    live_ctx.session.add_user_message("hello")
    out = text(dispatch(live_ctx, "/context")).lower()
    assert "token" in out or "токен" in out
    assert "window" in out or "окно" in out or "messages" in out or "сообщений" in out


# --- aliases -----------------------------------------------------------------

def test_usage_alias_answers_like_stats(live_ctx):
    out = text(dispatch(live_ctx, "/usage")).lower()
    assert "model" in out


def test_resume_without_args_lists_sessions(ctx):
    # Same door as /continue: no id means the session list, not a crash.
    out = dispatch(ctx, "/resume")
    assert out.output is not None
