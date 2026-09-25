import locale
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

_FALLBACK_ENCODING = locale.getpreferredencoding(False) or "utf-8"

# A command that prints half a gigabyte used to be held three times over — in the
# pipe buffer, in the decoded string, and again in the saved session — and the
# tokenizer then re-read all of it on every later turn. Nothing legitimate needs
# more than this, and the model is told exactly what was dropped.
MAX_CAPTURE = 2 * 1024 * 1024


def shell_command(command: str) -> list[str]:
    """Wrap a command for a shell that understands &&, ~ and mkdir -p.

    Windows' shell=True picks cmd.exe, which chokes on POSIX syntax, so use
    Git Bash when it is present. `-c`, not `-lc`: a login shell runs whatever the
    user put in ~/.bash_profile, which is code the model never asked for and
    nobody would think to look at when a command behaved strangely.
    """
    bash = _find_bash()
    if bash:
        return [bash, "-c", command]
    if os.name == "posix":
        # Termux installs bash as a package and Android ships /bin/sh, so a box
        # with no bash is normal there — and cmd.exe does not exist anywhere.
        return [shutil.which("sh") or "/bin/sh", "-c", command]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]


def _find_bash() -> str | None:
    """A bash that shares the filesystem the file tools are using.

    `shutil.which("bash")` answers first, and on a box with WSL installed that is
    C:\\Windows\\System32\\bash.exe — a second Linux root, a second $HOME, where
    `rm -rf ~/project` deletes something the read/write tools cannot even see.
    """
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    found = shutil.which("bash")
    if not found:
        return None
    system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
    try:
        if system32.exists() and Path(found).parent.samefile(system32):
            return None
    except OSError:
        pass                      # no shared filesystem to compare against
    return found


def decode(data: bytes) -> str:
    text = ""
    if data:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode(_FALLBACK_ENCODING, errors="replace")
    # cmd.exe and Windows Python children emit CRLF; stray \r breaks line-based
    # rendering in the UI.
    return text.replace("\r\n", "\n")


def kill_process_tree(process) -> None:
    """Kill the child *and its children*.

    `subprocess.run(timeout=…)` kills only the direct child, then waits on the
    pipes — which the grandchildren still hold open. Measured here: a 1-second
    timeout came back after 4.4 s, and the process the model believed stopped
    kept running (and writing files) long after "Command timed out after 1s".
    """
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           capture_output=True, timeout=10)
        else:
            import signal
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:
        # A failed kill must not turn into a second, more confusing error.
        try:
            process.kill()
        except Exception:
            pass


def run_argv(argv: list, timeout: int = 60, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run one argv with bounded output, and really stop it on timeout.

    Output goes to temporary files rather than pipes: a pipe has to be drained
    while the child writes or it deadlocks, and draining it means holding all of
    the child's noise in memory. The files also make the size limit honest — we
    read at most MAX_CAPTURE and say so.

    `env` is layered over the inherited environment for the handful of tools
    that need promises of their own — `git` switches off the pager, the system
    config and terminal prompts — and it is applied last, so a tool's guarantee
    cannot be argued out of existence by a value already sitting in the
    environment.
    """
    env_overrides = {key: str(value) for key, value in (env or {}).items()}
    base = {**os.environ, **env_overrides}
    # Child processes (python, git, pip) must speak UTF-8 too, or Russian text
    # comes back as mangled cp866 bytes.
    base["PYTHONIOENCODING"] = "utf-8"
    if not str(argv[0]).lower().endswith("cmd.exe"):
        base["LC_ALL"] = base.get("LC_ALL") or "C.UTF-8"
    popen_kwargs = {"env": base,
                    # A command that reads stdin must not eat the user's keystrokes.
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    out_fd, out_path = tempfile.mkstemp(prefix="beecode-out-")
    err_fd, err_path = tempfile.mkstemp(prefix="beecode-err-")
    try:
        with os.fdopen(out_fd, "wb") as out_file, os.fdopen(err_fd, "wb") as err_file:
            process = subprocess.Popen(argv, stdout=out_file, stderr=err_file,
                                       **popen_kwargs)
            try:
                process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                kill_process_tree(process)
                # Bounded, because a detached grandchild can keep the redirect
                # open and wait() would sit there forever with the agent frozen.
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
                raise
        return subprocess.CompletedProcess(argv, process.returncode,
                                           _read_capped(out_path), _read_capped(err_path))
    finally:
        for path in (out_path, err_path):
            try:
                os.remove(path)
            except OSError:
                pass


def _read_capped(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(MAX_CAPTURE + 1)[:MAX_CAPTURE + 1]


def run_shell(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    return run_argv(shell_command(command), timeout=timeout)


def run_text(command: str, timeout: int = 60) -> tuple[str, str, int]:
    """Run a command and return (stdout, stderr, returncode) as decoded text."""
    result = run_shell(command, timeout=timeout)
    return _capped(result.stdout), _capped(result.stderr), result.returncode


def run_argv_text(argv: list, timeout: int = 60,
                  env: dict | None = None) -> tuple[str, str, int]:
    """The same, for a command that must never reach a shell."""
    result = run_argv(argv, timeout=timeout, env=env)
    return _capped(result.stdout), _capped(result.stderr), result.returncode


def _capped(data: bytes) -> str:
    """Decoded output, plus the sentence saying the rest was dropped.

    `_read_capped` keeps one byte past the limit precisely so we can tell "the
    program printed exactly this" from "this is the head of something longer".
    Without the note a 5 MB build log reads to the model — and to the user — as
    if it ended where our buffer did.
    """
    return decode(data[:MAX_CAPTURE]) + truncate_note(data)


def truncate_note(data: bytes) -> str:
    """What to append when the child printed more than we keep."""
    if len(data) <= MAX_CAPTURE:
        return ""
    return (f"\n… output cut at {MAX_CAPTURE // 1024} KB — redirect it to a file "
            f"and read the part you need")
