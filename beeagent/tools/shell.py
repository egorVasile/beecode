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


def run_shell(command: str, timeout: int = 60) -> subprocess.CompletedProcess:
    argv = shell_command(command)
    env = dict(os.environ)
    # Child processes (python, git, pip) must speak UTF-8 too, or Russian text
    # comes back as mangled cp866 bytes.
    env["PYTHONIOENCODING"] = "utf-8"
    if not argv[0].lower().endswith("cmd.exe"):
        env["LC_ALL"] = env.get("LC_ALL") or "C.UTF-8"
    return subprocess.run(
        argv,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def run_text(command: str, timeout: int = 60) -> tuple[str, str, int]:
    """Run a command and return (stdout, stderr, returncode) as decoded text."""
    result = run_shell(command, timeout=timeout)
    return decode(result.stdout), decode(result.stderr), result.returncode
