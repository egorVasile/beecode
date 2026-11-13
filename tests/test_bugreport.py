"""`/bug` packs a report the user can send — and it must never pack a secret."""
import io

from rich.console import Console

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ui.commands import ReplContext, dispatch


class FakeAgent:
    def __init__(self, workdir):
        self.workdir = str(workdir)


def _ctx(project, session=None, config=None):
    return ReplContext(agent=FakeAgent(project),
                       config=config or BeeConfig(),
                       session=session or Session())


def _text(result) -> str:
    body = result.output
    if isinstance(body, str):
        return body
    buffer = io.StringIO()
    Console(file=buffer, width=200).print(body)
    return buffer.getvalue()


def test_bug_writes_a_report_with_session_tail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    session = Session()
    session.add_user_message("it broke when I typed /history")
    session.add_assistant_message("oops")
    out = _text(dispatch(_ctx(tmp_path, session), "/bug history crashed"))
    assert "bug-" in out and ".md" in out
    files = list((tmp_path / ".beeagent").glob("bug-*.md"))
    assert len(files) == 1
    body = files[0].read_text(encoding="utf-8")
    from beeagent import __version__

    assert "history crashed" in body
    assert "it broke when I typed" in body
    assert __version__ in body


def test_bug_never_carries_secrets(tmp_path, monkeypatch):
    """api_keys, pool_token and endpoint URLs stay out — counts stay in."""
    monkeypatch.chdir(tmp_path)
    config = BeeConfig(api_keys={"groq": "gsk_totally_live_secret_value"},
                       pool_token="seat-token-live")
    config.pool_url = "https://pool.internal.example:8443/x"
    dispatch(_ctx(tmp_path, config=config), "/bug")
    body = next((tmp_path / ".beeagent").glob("bug-*.md")).read_text(encoding="utf-8")
    assert "gsk_totally_live_secret_value" not in body
    assert "seat-token-live" not in body
    assert "pool.internal.example" not in body
    assert "own_keys_stored: 1" in body


def test_bug_without_a_note_still_writes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = _text(dispatch(_ctx(tmp_path), "/bug")).lower()
    assert "bug-" in out
    assert "прочитай" in out or "read it" in out
