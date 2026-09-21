import locale
import os
import shutil
import subprocess
from pathlib import Path

_FALLBACK_ENCODING = locale.getpreferredencoding(False) or "utf-8"


def shell_command(command: str) -> list[str]:
    """Wrap a command for a shell that understands &&, ~ and mkdir -p.

    Windows' shell=True picks cmd.exe, which chokes on POSIX syntax, so use
    Git Bash when it is present.
    """
    bash = _find_bash()
    if bash:
        return [bash, "-lc", command]
    return [os.environ.get("COMSPEC", "cmd.exe"), "/c", command]


def _find_bash() -> str | None:
    found = shutil.which("bash")
    if found:
        return found
    for candidate in (
        Path(r"C:\Program Files\Git\bin\bash.exe"),
        Path(r"C:\Program Files (x86)\Git\bin\bash.exe"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


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


def _kill_tree(process) -> None:
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


def run_shell(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    argv = shell_command(command)
    env = dict(os.environ)
    # Child processes (python, git, pip) must speak UTF-8 too, or Russian text
    # comes back as mangled cp866 bytes.
    env["PYTHONIOENCODING"] = "utf-8"
    if not argv[0].lower().endswith("cmd.exe"):
        env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    popen_kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "env": env,
                    # A command that reads stdin must not eat the user's keystrokes.
                    "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True
    process = subprocess.Popen(argv, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(process)
        stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def run_text(command: str, timeout: int = 60) -> tuple[str, str, int]:
    """Run a command and return (stdout, stderr, returncode) as decoded text."""
    result = run_shell(command, timeout=timeout)
    return decode(result.stdout), decode(result.stderr), result.returncode
