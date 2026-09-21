"""`beecode --update` has to work from both install shapes."""
import subprocess

from beeagent.cli import update_self


def test_a_checkout_is_pulled_not_reinstalled(tmp_path, monkeypatch, capsys):
    """pip-installing over an editable checkout would freeze the dev copy."""
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or 0)
    (tmp_path / ".git").mkdir()

    assert update_self(root=tmp_path) == 0
    assert calls == [["git", "-C", str(tmp_path), "pull", "--ff-only"]]


def test_an_installed_copy_is_upgraded_from_the_repository(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or 0)

    assert update_self(root=tmp_path) == 0
    command = calls[0]
    assert command[1:4] == ["-m", "pip", "install"]
    assert command[-1] == "git+https://github.com/egorVasile/beecode.git"
