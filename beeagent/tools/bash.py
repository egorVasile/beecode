import subprocess
import threading

from .base import BaseTool, ToolResult
from .shell import Run, capped_text, run_shell

# Models send this as "5", as null, as -5, or as 10**12. Each of those used to
# reach subprocess verbatim: a string crashed the tool, null disabled the
# timeout entirely, a negative number timed out instantly, and a huge one died
# in `timestamp out of range` — so a command the user allowed never ran.
def _seconds(value) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = 60
    if seconds <= 0:
        # Zero and negatives are the model not meaning "fast" — that reads as
        # "no idea", which is the default, not the smallest value in the range.
        seconds = 60
    return min(seconds, 1800)


# What the model is sent back once the human has watched the command print it.
# Every line of a long build log is already on the user's screen; pasting all of
# it into the next prompt spends the context window on a replay. The tail is what
# a failure actually explains, so below this size the model still gets everything
# — a 20-line result is not worth a note about it.
LIVE_TRANSCRIPT_LIMIT = 4000
LIVE_TAIL_LINES = 30

# A stopped command has to say so. Reading forty lines of a killed `pytest` as a
# finished run is how an agent goes on to report a suite as passing.
STOPPED_NOTE = ("\n… stopped early: the command was interrupted, so this is only "
                "what it printed before it was killed — it did not finish")


class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a shell command: builds, tests, installs, docker, git plumbing. Do not use it for "
        "files — read, write, edit, grep, glob and list_directory exist for that. Quote paths "
        "that contain spaces, and chain steps that must happen in order with && ."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60},
        },
        "required": ["command"],
    }
    # Set by the loop before execute(), exactly like `granted`: a callable taking
    # ("out" | "err", text) for every line the command prints, or None. With it,
    # a three-minute test suite stops being three minutes of silence; without it
    # nothing is read early, and the result is the bytes it has always been.
    on_output = None

    def __init__(self) -> None:
        # One command runs per tool instance at a time, and `interrupt()` may be
        # called from the UI thread while it runs. The lock covers the reference
        # only — no wait, no child, no I/O inside — so the calling thread cannot
        # be held up by the command it is trying to stop.
        self._lock = threading.Lock()
        self._run = None

    def interrupt(self) -> bool:
        """Stop the command this tool is running. True if one was running.

        Returns immediately: the tree kill happens in the worker watching the
        command, within one poll of this call.
        """
        with self._lock:
            run = self._run
        if run is None:
            return False
        return run.interrupt()

    def execute(self, command: str, timeout=60) -> ToolResult:
        seconds = _seconds(timeout)
        sink = self.on_output
        if not callable(sink):
            sink = None
        run = Run()
        with self._lock:
            self._run = run
        # Counted here rather than taken from the reader: this is the number of
        # lines the *user* actually saw, which is what decides what the model is
        # sent — and it only counts a line the sink accepted.
        seen = [0]

        def on_line(kind: str, text: str) -> None:
            sink(kind, text)
            seen[0] += 1

        try:
            try:
                result = run_shell(command, timeout=seconds,
                                   on_line=on_line if sink is not None else None,
                                   run=run)
            except subprocess.TimeoutExpired:
                return ToolResult(
                    output=f"Command timed out after {seconds}s" + _shown_note(seen[0]),
                    error=True)
            except Exception as e:
                return ToolResult(output=str(e), error=True)
        finally:
            with self._lock:
                if self._run is run:
                    self._run = None

        # The human saw these lines already. Sent in full they would be in the
        # transcript twice, so a large streamed run leaves the model the tail of
        # each stream and the count of what it is not being shown.
        streamed = seen[0] > 0
        keep = (LIVE_TAIL_LINES
                if streamed and len(result.stdout) + len(result.stderr) > LIVE_TRANSCRIPT_LIMIT
                else 0)
        stdout = capped_text(result.stdout, keep)
        stderr = capped_text(result.stderr, keep)
        output = stdout
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        returncode = result.returncode if result.returncode is not None else 1
        if result.interrupted:
            # A killed command is not a completed one, whatever its exit code
            # happened to be on the way out.
            output = (output or "(no output)") + STOPPED_NOTE
        return ToolResult(
            output=output or "(no output)",
            error=returncode != 0 or result.interrupted,
            metadata={"returncode": returncode, "pid": run.pid,
                      "interrupted": result.interrupted},
        )

    def is_safe(self) -> bool:
        return False


def _shown_note(lines: int) -> str:
    """What the timeout swallowed after the user had already seen it."""
    if lines <= 0:
        return ""
    return (f"\n… {lines} line(s) printed before the timeout were shown live and "
            f"are not repeated here")
