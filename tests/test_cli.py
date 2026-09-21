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
    # The version in pyproject does not move between commits, so without
    # --force-reinstall pip calls the installed copy "already satisfied":
    # --update then refreshes g4f and leaves BeeCode itself untouched.
    assert "--force-reinstall" in command
    assert "--no-deps" in command, "the app is replaced first, the dependencies after"
    assert len(calls) == 2, "a second pass brings the dependencies forward"
    assert "--force-reinstall" not in calls[1]


def test_a_console_script_shim_is_not_used_as_an_interpreter(tmp_path, monkeypatch):
    """`beecode.exe -m pip …` is nonsense; the venv's python sits next to it."""
    import os
    import sys

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "beecode.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "beecode.exe"))

    from beeagent.cli import _pip_python

    interpreter = "python.exe" if os.name == "nt" else "python"
    assert _pip_python() == ""            # no interpreter next to it
    (scripts / interpreter).write_bytes(b"")
    assert _pip_python() == str(scripts / interpreter)


def test_updating_from_the_running_exe_refuses_on_windows(tmp_path, monkeypatch):
    """Windows locks a running image: pip would uninstall the package and then
    fail to write the launcher back, leaving an install that cannot start."""
    import os
    import sys

    scripts = tmp_path / "Scripts"
    scripts.mkdir()
    (scripts / "beecode.exe").write_bytes(b"")
    (scripts / "python.exe").write_bytes(b"")
    monkeypatch.setattr(sys, "executable", str(scripts / "beecode.exe"))
    monkeypatch.setattr(os, "name", "nt")
    calls = []
    monkeypatch.setattr(subprocess, "call", lambda argv, **kw: calls.append(argv) or 0)

    assert update_self(root=tmp_path) == 1
    assert calls == [], "it must not start a pip run it cannot finish"
