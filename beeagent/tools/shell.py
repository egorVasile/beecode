import locale
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

_FALLBACK_ENCODING = locale.getpreferredencoding(False) or "utf-8"

# A command that prints half a gigabyte used to be held three times over — in the
# pipe buffer, in the decoded string, and again in the saved session — and the
# tokenizer then re-read all of it on every later turn. Nothing legitimate needs
# more than this, and the model is told exactly what was dropped.
MAX_CAPTURE = 2 * 1024 * 1024

# --- watching a command that is still running ---------------------------------
# A watched command is polled, not blocked on: one pass every WATCH_INTERVAL
# seconds is what lets a stop request from the UI thread land within ~50 ms
# without that thread ever touching the child.
WATCH_INTERVAL = 0.05
# Bytes taken from a redirect file per read. Chunked, because the reader's cost
# has to be per *buffer*, not per byte: 50 000 short lines are a handful of reads
# and one callback each, never 50 000 syscalls' worth of byte-at-a-time work.
READ_CHUNK = 64 * 1024
# The most text held back waiting for a newline that never comes. A program that
# prints one enormous line is handed over in bounded pieces rather than kept.
MAX_PARTIAL = 64 * 1024
# Reads per stream per pass (~40 MB/s): enough that no real build outruns the
# reader for long, small enough that one pass cannot starve the deadline check.
PASSES_PER_READ = 32
# The last pass, after the writer is gone: drain the file, bounded in case a
# grandchild we did not own is still appending to it.
DRAIN_AT_EXIT = 256


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


def run_argv(argv: list, timeout: int = 60, env: dict | None = None,
             on_line=None, run: "Run | None" = None) -> "ShellResult":
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

    `on_line` and `run` are what makes a long command visible while it runs, and
    they change nothing when both are absent: the exact same wait, the exact same
    bytes. With them, the two redirect files are *also* read from the front
    through a second, read-only handle as the child appends to them. A regular
    file has no buffer to fill and never blocks a writer, so this cannot
    reintroduce the deadlock the temp files exist to avoid, and the reader holds
    at most one chunk plus one unfinished line at a time, so watching a noisy
    command costs no more memory than not watching it. The capture handed back is
    still read from the file once the child is gone, exactly as before.

    Note what streaming cannot promise: the child decides when its bytes reach the
    file. Python, git and pytest flush, and their progress appears as it happens;
    a program that block-buffers its stdout shows its lines in runs of a few
    kilobytes. That is the child's choice, and a pipe would not change it.
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
            stopped_early = False
            if on_line is None and run is None:
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    kill_process_tree(process)
                    _settle(process)
                    raise
            else:
                if run is not None:
                    run._started(process)
                try:
                    stopped_early = _watch(process, argv, timeout, on_line, run,
                                           (out_path, err_path))
                finally:
                    if run is not None:
                        run._stopped()
        return ShellResult(argv, process.returncode,
                           _read_capped(out_path), _read_capped(err_path),
                           interrupted=stopped_early)
    finally:
        for path in (out_path, err_path):
            _discard(path)


def _discard(path: str) -> None:
    """Delete a redirect file, allowing for the moment Windows needs to let go.

    A killed child's handles are released by the kernel *after* the wait returns,
    so the first `os.remove` after a tree kill can fail on a file nothing is
    really using any more. A few tries over ~0.1 s is enough; a file that a
    genuine survivor still holds is left behind quietly, as it always was, rather
    than stalling the tool that is only trying to tidy up.
    """
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except OSError:
            if attempt < 4:
                time.sleep(0.02)


def _settle(process) -> None:
    """Give a killed child a bounded moment to actually be gone.

    Bounded, because a detached grandchild can keep the redirect open and an
    unbounded `wait()` would sit there forever with the agent frozen.
    """
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        pass


class ShellResult(subprocess.CompletedProcess):
    """A `CompletedProcess` that also says *how* the run ended.

    `interrupted` is the difference between "this is what the command printed"
    and "this is all it managed to print before it was stopped" — a distinction
    a tool must not quietly drop, because the second text is not an answer.
    """

    def __init__(self, args, returncode, stdout, stderr, interrupted=False):
        super().__init__(args, returncode, stdout, stderr)
        self.interrupted = interrupted


class Run:
    """What another thread may safely ask of a command that is still running.

    `interrupt()` sets an event and returns immediately: the UI thread that calls
    it never touches the child, never runs `taskkill`, and never waits on a lock
    that is held across I/O. The worker watching the command notices the flag on
    its next pass and does the killing in its own thread, where blocking is
    allowed. It is deliberately not a `KeyboardInterrupt` thrown into the worker:
    that lands wherever the interpreter happens to be, and leaves the child
    running — which is the opposite of what the user just asked for.
    """

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._process = None
        self.pid = None

    def _started(self, process) -> None:
        with self._lock:
            self._process = process
            # Named here because a caller that wants to prove the child is really
            # gone — the orphan test in tests/test_bash_stream.py — cannot ask a
            # finished Popen for anything once the files are cleaned up.
            self.pid = process.pid

    def _stopped(self) -> None:
        with self._lock:
            self._process = None

    @property
    def stop_requested(self) -> bool:
        return self._stop.is_set()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._process is not None

    def interrupt(self) -> bool:
        """Ask the command to stop. True if this call is the one that asked.

        The lock is held for two flag tests and no I/O, so a UI thread calling
        this never queues behind the worker; it is here so that two threads
        asking at once cannot both be told they were the one that stopped it.
        """
        with self._lock:
            already = self._stop.is_set()
            self._stop.set()
        return not already


def _watch(process, argv, timeout, on_line, run, paths) -> bool:
    """Tail the child's output until it ends, the clock runs out, or we are told.

    Returns True when a `Run.interrupt()` ended the run. Raises TimeoutExpired
    after the same tree kill `process.wait(timeout=…)` would have, so a command
    that hangs is stopped and reported exactly as it is today.
    """
    tailers = [] if on_line is None else [
        _Tailer("out", paths[0], on_line), _Tailer("err", paths[1], on_line)]
    deadline = None if timeout is None else time.monotonic() + timeout
    interrupted = False
    timed_out = False
    try:
        while True:
            for tailer in tailers:
                tailer.pump(PASSES_PER_READ)
            if process.poll() is not None:
                break                   # it finished by itself: nobody stopped it
            if run is not None and run.stop_requested:
                kill_process_tree(process)
                _settle(process)
                interrupted = True
                break
            if deadline is not None and time.monotonic() >= deadline:
                kill_process_tree(process)
                _settle(process)
                timed_out = True
                break
            time.sleep(WATCH_INTERVAL)
    finally:
        # Closed here, before the caller removes the files: on Windows an open
        # reader is enough to make that removal fail and litter the temp folder.
        for tailer in tailers:
            tailer.finish(DRAIN_AT_EXIT)
    if timed_out:
        raise subprocess.TimeoutExpired(argv, timeout)
    return interrupted


class _Tailer:
    """Whole lines out of a redirect file the child is still appending to.

    A line is decoded only once its newline has arrived, which is also what keeps
    a multi-byte character split across two reads from ever being decoded in
    halves. Callbacks are counted, never queued: an undeliverable line does not
    slow or resize the capture, because the capture is read off the file at the
    end and not out of this object.
    """

    def __init__(self, kind: str, path: str, on_line) -> None:
        self.kind = kind
        self.on_line = on_line
        self.pending = b""
        self.lines = 0
        self.callback_broke = False
        try:
            self.handle = open(path, "rb", buffering=0)
        except OSError:
            self.handle = None      # blind, not stuck: the child still runs

    def pump(self, limit: int) -> int:
        """Take what the file has grown by, at most *limit* chunks. Line count so far."""
        if self.handle is None:
            return self.lines
        for _ in range(limit):
            try:
                chunk = self.handle.read(READ_CHUNK)
            except OSError:
                break
            if not chunk:
                break
            self._absorb(chunk)
        return self.lines

    def finish(self, limit: int) -> None:
        self.pump(limit)
        if self.pending:
            # The last line of a command rarely ends with a newline; it still has
            # to reach the user, or the result looks like it was cut off.
            self._emit(self.pending)
            self.pending = b""
        self.close()

    def close(self) -> None:
        if self.handle is not None:
            try:
                self.handle.close()
            except OSError:
                pass
            self.handle = None

    def _absorb(self, chunk: bytes) -> None:
        data = self.pending + chunk if self.pending else chunk
        parts = data.split(b"\n")
        for raw in parts[:-1]:
            self._emit(raw)
        self.pending = parts[-1]
        # A line with no newline in sight is handed over in bounded pieces, so the
        # reader's footprint is MAX_PARTIAL whatever the child decides to print.
        while len(self.pending) > MAX_PARTIAL:
            self._flush_partial()

    def _flush_partial(self) -> None:
        """Hand over a bounded piece of a line that has no end in sight."""
        cut, text = MAX_PARTIAL, None
        for _ in range(4):            # a UTF-8 sequence is at most four bytes
            try:
                text = self.pending[:cut].decode("utf-8")
                break
            except UnicodeDecodeError:
                cut -= 1
        if text is None:
            cut, text = MAX_PARTIAL, decode(self.pending[:MAX_PARTIAL])
        self._emit_text(text)
        self.pending = self.pending[cut:]

    def _emit(self, raw: bytes) -> None:
        # cmd.exe and Windows Python children end lines with CRLF; the newline
        # itself is gone already, so all that is left to drop is the carriage
        # return — exactly what `decode` does for the whole capture.
        if raw.endswith(b"\r"):
            raw = raw[:-1]
        self._emit_text(decode(raw))

    def _emit_text(self, text: str) -> None:
        if self.on_line is None or self.callback_broke:
            return
        self.lines += 1
        try:
            self.on_line(self.kind, text)
        except Exception:
            # A UI that cannot take a line must not become a command that failed.
            self.callback_broke = True


def _read_capped(path: str) -> bytes:
    with open(path, "rb") as handle:
        return handle.read(MAX_CAPTURE + 1)[:MAX_CAPTURE + 1]


def run_shell(command: str, timeout: int = 60, env: dict | None = None,
              on_line=None, run: "Run | None" = None) -> ShellResult:
    return run_argv(shell_command(command), timeout=timeout, env=env,
                    on_line=on_line, run=run)


def run_text(command: str, timeout: int = 60) -> tuple[str, str, int]:
    """Run a command and return (stdout, stderr, returncode) as decoded text."""
    result = run_shell(command, timeout=timeout)
    return capped_text(result.stdout), capped_text(result.stderr), result.returncode


def run_argv_text(argv: list, timeout: int = 60,
                  env: dict | None = None) -> tuple[str, str, int]:
    """The same, for a command that must never reach a shell."""
    result = run_argv(argv, timeout=timeout, env=env)
    return capped_text(result.stdout), capped_text(result.stderr), result.returncode


def capped_text(data: bytes, keep_last: int = 0) -> str:
    """Decoded output, plus the sentence saying the rest was dropped.

    `_read_capped` keeps one byte past the limit precisely so we can tell "the
    program printed exactly this" from "this is the head of something longer".
    Without the note a 5 MB build log reads to the model — and to the user — as
    if it ended where our buffer did.

    `keep_last` is for a command whose lines the human already watched arrive:
    the text shrinks to its last N lines and says how many it dropped, instead of
    being pasted into the transcript a second time.
    """
    body = decode(data[:MAX_CAPTURE])
    note = truncate_note(data)
    if keep_last > 0:
        lines = body.splitlines()
        if len(lines) > keep_last:
            dropped = len(lines) - keep_last
            body = "\n".join(lines[-keep_last:]) + "\n"
            note = (f"\n… {dropped} earlier line(s) not repeated here — they were "
                    f"shown as the command printed them" + note)
    return body + note


def truncate_note(data: bytes) -> str:
    """What to append when the child printed more than we keep."""
    if len(data) <= MAX_CAPTURE:
        return ""
    return (f"\n… output cut at {MAX_CAPTURE // 1024} KB — redirect it to a file "
            f"and read the part you need")
