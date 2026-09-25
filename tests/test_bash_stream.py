"""A command that is still running is not a command with nothing to say.

Three incidents shaped this file.

1. A three-minute test suite printed nothing for three minutes and then printed
   all of it, because the child's output was only read after it exited. The tests
   below pin the *ordering*: a line has to reach the callback while the child is
   provably still alive, not merely arrive at some point before the call ended.
2. The reason the old code writes to temp files instead of pipes is written at the
   top of `shell.py`: an undrained pipe deadlocks the child, and a drained one
   holds the whole noise in memory. Streaming is built on the same two files, so
   these tests also pin the properties that made that choice right — a 50 000-line
   command is handed over in bounded pieces, and the capture limit still bites.
3. A runaway command used to mean killing the agent. `interrupt()` is therefore
   called from a second thread, the way the UI thread will call it, and the proof
   is not that the call returned but that the process tree is gone afterwards.
"""
import os
import subprocess
import sys
import tempfile
import threading
import time
import tracemalloc

import pytest

from beeagent.tools.bash import LIVE_TAIL_LINES, BashTool
from beeagent.tools.shell import (MAX_CAPTURE, MAX_PARTIAL, Run, capped_text,
                                  run_argv, run_text, shell_command)

# Long enough that a command which is merely slow is not mistaken for a hung one,
# short enough that a broken stop cannot stall the suite: nothing here sleeps for
# real, the children sleep and are interrupted or time out.
BUDGET = 60


def _posix_shell() -> bool:
    return shell_command("true")[0].lower().endswith(("bash", "bash.exe", "sh"))


def _command(script) -> str:
    """The tool's own way to name a child script: quoted for this shell.

    Forward slashes throughout, because a backslash inside double quotes is an
    escape in bash and a path separator in cmd, and the same string has to mean
    the same thing in both.
    """
    return f'"{sys.executable}" "{os.path.abspath(str(script)).replace(os.sep, "/")}"'


def _argv(script) -> list:
    return [sys.executable, str(script)]


def _script(tmp_path, name, source):
    """A child program, flushed by hand: the child decides when a line is readable."""
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return path


def _run(script, **kwargs):
    return run_argv(_argv(script), **kwargs)


def _pid_alive(pid):
    """Is this process still a process? Windows has no wait4(), so ask the OS.

    A reaped-or-not zombie is not a survivor: it cannot write a byte, and on POSIX
    a killed grandchild is one for as long as init takes to collect it, so the
    state is checked where the kernel exposes it.
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)     # QUERY_LIMITED
        if not handle:
            return False                                      # the pid is gone
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                          # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
        return fields[0] != b"Z"                              # 'Z'ombie: already dead
    except (FileNotFoundError, IndexError, OSError):
        pass                                                  # no /proc to ask
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                                           # alive, not mine to signal
    return True


def _gone(pid, seconds=3.0) -> bool:
    """Wait out the kernel's bookkeeping before calling a PID a survivor."""
    deadline = time.time() + seconds
    while _pid_alive(pid):
        if time.time() >= deadline:
            return False
        time.sleep(0.1)
    return True


# ------------------------------------------------------------------ live output ---

def test_a_line_arrives_while_the_child_is_still_running(tmp_path):
    """The proof is causal, not a stopwatch.

    The child waits for a flag file before it prints its second line, and the flag
    is written from inside the callback for the first one. Deferred output cannot
    pass this test at any speed: nothing would ever set the flag, and the child
    would sit out its own patience.
    """
    flag = tmp_path / "flag"
    script = _script(tmp_path, "causal.py", (
        "import os, sys, time\n"
        "print('ready', flush=True)\n"
        f"flag = {str(flag)!r}\n"
        "for _ in range(100):\n"
        "    if os.path.exists(flag):\n"
        "        break\n"
        "    time.sleep(0.05)\n"
        "print('the parent answered while I was still alive', flush=True)\n"
    ))
    arrived = []

    def on_line(kind, text):
        arrived.append((kind, text))
        if text == "ready":
            flag.write_text("go", encoding="utf-8")

    started = time.time()
    result = _run(script, timeout=BUDGET, on_line=on_line)
    assert result.returncode == 0, result.stderr
    assert [text for _, text in arrived] == [
        "ready", "the parent answered while I was still alive"], arrived
    # The child's own patience is five seconds; the exchange finished inside it.
    assert time.time() - started < 5


def test_the_first_line_lands_long_before_the_process_ends(tmp_path):
    """And the timestamps, because "the callback was called" passes on a deferred read."""
    script = _script(tmp_path, "sleepy.py", (
        "import sys, time\n"
        "print('first', flush=True)\n"
        "time.sleep(1.5)\n"
        "print('second', flush=True)\n"
    ))
    clock = []
    started = time.monotonic()
    _run(script, timeout=BUDGET,
         on_line=lambda kind, text: clock.append((text, time.monotonic() - started)))
    took = time.monotonic() - started

    assert [text for text, _ in clock] == ["first", "second"]
    first, second = clock[0][1], clock[1][1]
    assert first < took / 2, f"the first line waited {first:.2f}s of a {took:.2f}s run"
    gap = second - first
    assert gap >= 1.2, f"both lines arrived together ({gap:.2f}s apart): nothing streamed"


def test_stdout_and_stderr_keep_separate_kinds(tmp_path):
    script = _script(tmp_path, "both.py", (
        "import sys\n"
        "print('out one', flush=True)\n"
        "print('err one', file=sys.stderr, flush=True)\n"
        "print('out two', flush=True)\n"
    ))
    got = []
    _run(script, timeout=BUDGET, on_line=lambda kind, text: got.append((kind, text)))
    assert ("out", "out one") in got and ("out", "out two") in got
    assert ("err", "err one") in got
    assert [kind for kind, _ in got].count("err") == 1, got


def test_a_last_line_with_no_newline_is_still_delivered(tmp_path):
    """`printf` without a newline, a compiler mid-file: the common case is the tail."""
    script = _script(tmp_path, "tail.py", (
        "import sys\n"
        "print('whole line', flush=True)\n"
        "sys.stdout.write('no newline at all')\n"
        "sys.stdout.flush()\n"
    ))
    got = []
    _run(script, timeout=BUDGET, on_line=lambda kind, text: got.append(text))
    assert got == ["whole line", "no newline at all"], got


def test_windows_line_endings_do_not_reach_the_callback(tmp_path):
    """A child that speaks CRLF must not hand the UI a string ending in \\r."""
    script = _script(tmp_path, "crlf.py", (
        "import sys\n"
        "sys.stdout.buffer.write(b'one\\r\\ntwo\\r\\n')\n"
        "sys.stdout.buffer.flush()\n"
    ))
    got = []
    _run(script, timeout=BUDGET, on_line=lambda kind, text: got.append(text))
    assert got == ["one", "two"], got


def test_cyrillic_survives_the_line_split(tmp_path):
    """A line is decoded only once its newline has landed, so a split read cannot mangle it."""
    script = _script(tmp_path, "cyr.py", "print('привет пчела', flush=True)\n")
    got = []
    _run(script, timeout=BUDGET, on_line=lambda kind, text: got.append(text))
    assert got == ["привет пчела"]


def test_a_line_longer_than_the_cap_arrives_in_pieces(tmp_path):
    """One 200 KB line is several bounded callbacks, never one 200 KB string."""
    size = MAX_PARTIAL * 3 + 17
    script = _script(tmp_path, "long.py", (
        "import sys\n"
        f"sys.stdout.write('x' * {size})\n"
        "sys.stdout.flush()\n"
    ))
    pieces = []
    _run(script, timeout=BUDGET, on_line=lambda kind, text: pieces.append(text))
    assert len(pieces) >= 4, f"one huge line came back as {len(pieces)} callback(s)"
    assert max(len(piece) for piece in pieces) <= MAX_PARTIAL
    assert sum(len(piece) for piece in pieces) == size, "the pieces lost text"


# ------------------------------------------------------------------- the cap ---

def test_fifty_thousand_lines_cost_the_reader_nothing_per_byte(tmp_path):
    """More than the capture holds, delivered whole, with a bounded reader."""
    lines = 50_000
    script = _script(tmp_path, "many.py", (
        "import sys\n"
        f"for i in range({lines}):\n"
        "    sys.stdout.write(f'{i:05d} ' + 'x' * 40 + chr(10))\n"
        "    if i % 200 == 0:\n"
        "        sys.stdout.flush()\n"
        "sys.stdout.flush()\n"
    ))
    counted = [0]
    widest = [0]

    def on_line(kind, text):
        counted[0] += 1
        widest[0] = max(widest[0], len(text))

    tracemalloc.start()
    try:
        result = _run(script, timeout=120, on_line=on_line)
        streamed_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()

    assert counted[0] == lines, f"{counted[0]} of {lines} lines reached the callback"
    assert widest[0] <= MAX_PARTIAL, "the reader handed over an unbounded line"
    # The capture rule still applies to a streamed run, and still says so: the
    # file held more than we keep.
    assert len(result.stdout) == MAX_CAPTURE + 1
    assert capped_text(result.stdout).rstrip().endswith("read the part you need"), \
        "the cap was applied silently"

    tracemalloc.start()
    try:
        _run(script, timeout=120)
        plain_peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    extra = streamed_peak - plain_peak
    assert extra < 8 * 1024 * 1024, f"streaming added {extra / 1e6:.1f} MB of peak memory"


def test_a_flood_that_never_stops_is_still_stopped_by_the_timeout(tmp_path):
    """The reader must not become the reason a timed-out command keeps running."""
    script = _script(tmp_path, "flood.py", (
        "import sys, time\n"
        "while True:\n"
        "    sys.stdout.write('y' * 4000 + chr(10))\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.002)\n"
    ))
    seen = [0]
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        _run(script, timeout=2,
             on_line=lambda kind, text: seen.__setitem__(0, seen[0] + 1))
    took = time.monotonic() - started
    assert took < 12, f"the timeout did not stop the flood: {took:.1f}s"
    assert seen[0] > 100, "nothing arrived before the clock ran out"


# --------------------------------------------------------------- interruption ---

def test_interrupt_stops_a_sleeping_command_and_keeps_what_arrived(tmp_path):
    script = _script(tmp_path, "sleeper.py", (
        "import sys, time\n"
        "print('useful progress', flush=True)\n"
        "time.sleep(30)\n"
        "print('never reaches you', flush=True)\n"
    ))
    run = Run()
    got = []
    asked = []
    thread = threading.Timer(0.6, lambda: asked.append(run.interrupt()))
    thread.daemon = True
    thread.start()

    started = time.monotonic()
    result = _run(script, timeout=120, on_line=lambda kind, text: got.append(text), run=run)
    took = time.monotonic() - started

    assert asked == [True], "the stop request was refused by a run that was live"
    assert result.interrupted is True
    assert took < 15, f"the stop took {took:.1f}s to take effect"
    assert got == ["useful progress"], got
    assert result.returncode != 0


def test_a_stop_asked_before_the_command_starts_is_not_lost(tmp_path):
    """The UI can beat the worker to it: the flag is set, so the first pass stops it."""
    script = _script(tmp_path, "late.py", (
        "import sys, time\n"
        "print('first', flush=True)\n"
        "time.sleep(20)\n"
    ))
    run = Run()
    assert run.running is False
    assert run.interrupt() is True
    result = _run(script, timeout=120, on_line=lambda kind, text: None, run=run)
    assert result.interrupted is True
    assert result.returncode != 0


def test_an_idle_run_answers_a_second_stop_with_false():
    run = Run()
    assert run.interrupt() is True            # the first call is the one that asked
    assert run.interrupt() is False           # a second says nothing new
    assert run.stop_requested is True


def test_interrupt_leaves_no_survivor_of_its_own(tmp_path):
    """Not "the call returned" — the process really is gone, tree included.

    A stop that only killed the direct child left the build running behind a
    message that said it had stopped, which is what
    `subprocess.run(timeout=…)` did here for years. Checked by PID on Windows, and
    by the grandchild's own heartbeat everywhere, because a recycled PID would
    answer the first question either way.
    """
    heartbeat = tmp_path / "beat.log"
    ticker = _script(tmp_path, "beat.py", (
        "import sys, time\n"
        "while True:\n"
        "    with open(sys.argv[1], 'a') as fh:\n"
        "        fh.write('tick\\n')\n"
        "    time.sleep(0.1)\n"
    ))
    parent = _script(tmp_path, "spawner.py", (
        "import subprocess, sys, time\n"
        # No new session, no new group: a build tool's compiler inherits the
        # group it was born into, and that is what makes killing the *tree* the
        # difference between stopping a command and stopping a promise.
        f"child = subprocess.Popen([sys.executable, {str(ticker)!r}, {str(heartbeat)!r}],\n"
        "    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n"
    ))

    run = Run()
    seen = []
    threading.Timer(1.2, run.interrupt).start()
    result = _run(parent, timeout=120, run=run,
                  on_line=lambda kind, text: seen.append(text))
    assert result.interrupted is True
    assert seen and seen[0].strip().isdigit(), f"no grandchild pid was printed: {seen}"

    direct_pid, grandchild_pid = run.pid, int(seen[0])
    assert _gone(direct_pid), f"the direct child {direct_pid} outlived the stop"
    assert _gone(grandchild_pid), f"orphan {grandchild_pid} is still alive"

    beats = heartbeat.read_text(encoding="utf-8").count("tick")
    assert beats >= 2, f"the grandchild never ran ({beats} beats): the test proves nothing"
    time.sleep(1.0)
    assert heartbeat.read_text(encoding="utf-8").count("tick") == beats, \
        f"orphan {grandchild_pid} kept writing after the stop"


def test_the_stop_call_itself_is_instant(tmp_path):
    """The UI thread asks and goes back to drawing; the worker does the killing."""
    script = _script(tmp_path, "instant.py", (
        "import sys, time\n"
        "print('up', flush=True)\n"
        "time.sleep(30)\n"
    ))
    run = Run()
    measured = {}

    def ask():
        started = time.perf_counter()
        measured["value"] = run.interrupt()
        measured["took"] = time.perf_counter() - started

    def on_line(kind, text):
        # Started by the child's own first line, so the measurement never races
        # the run either starting or ending.
        threading.Thread(target=ask, daemon=True).start()

    _run(script, timeout=120, on_line=on_line, run=run)
    deadline = time.time() + 5
    while "took" not in measured and time.time() < deadline:
        time.sleep(0.05)
    assert measured["value"] is True
    assert measured["took"] < 0.05, f"the caller waited {measured['took'] * 1e3:.1f} ms"
    assert run.running is False, "the run was never detached when the command ended"


def test_stopping_a_command_that_said_nothing_is_still_a_clean_result(tmp_path):
    script = _script(tmp_path, "silent.py", "import time\ntime.sleep(30)\n")
    run = Run()
    threading.Timer(0.5, run.interrupt).start()
    result = _run(script, timeout=120, run=run)
    assert result.interrupted is True
    assert result.stdout == b"" and result.stderr == b""


# ------------------------------------------------------------------ the tool ---

@pytest.fixture
def tool():
    return BashTool()


def test_the_tool_streams_through_on_output(tool, tmp_path):
    script = _script(tmp_path, "tool_stream.py", (
        "import sys, time\n"
        "print('line one', flush=True)\n"
        "time.sleep(1.0)\n"
        "print('line two', flush=True)\n"
    ))
    seen = []
    tool.on_output = lambda kind, text: seen.append((text, time.monotonic()))
    result = tool.execute(_command(script), timeout=BUDGET)

    assert not result.error, result.output
    assert [text for text, _ in seen] == ["line one", "line two"]
    assert seen[1][1] - seen[0][1] >= 0.7, f"the user saw both lines at once: {seen}"
    assert result.output == "line one\nline two\n"


def test_a_stopped_command_says_so_in_the_result_the_model_reads(tool, tmp_path):
    script = _script(tmp_path, "tool_stop.py", (
        "import sys, time\n"
        "print('half a build', flush=True)\n"
        "time.sleep(30)\n"
    ))
    tool.on_output = lambda kind, text: None
    threading.Timer(0.8, tool.interrupt).start()
    result = tool.execute(_command(script), timeout=120)

    assert result.error is True
    assert result.metadata["interrupted"] is True
    assert result.metadata["pid"], "the result could not name the process it stopped"
    assert tool.interrupt() is False, "the finished run was still reachable from the tool"
    assert "half a build" in result.output
    assert "stopped early" in result.output


def test_a_stopped_command_says_so_even_when_it_never_spoke(tool, tmp_path):
    script = _script(tmp_path, "quiet_sleeper.py", "import time\ntime.sleep(30)\n")
    box = {}

    def run_it():
        box["result"] = tool.execute(_command(script), timeout=120)

    thread = threading.Thread(target=run_it)
    thread.start()
    time.sleep(0.8)
    assert tool.interrupt() is True, "nothing was running to stop"
    thread.join(timeout=20)
    assert not thread.is_alive(), "the tool never came back"
    result = box["result"]
    assert result.error and result.metadata["interrupted"]
    assert "stopped early" in result.output
    assert result.output.startswith("(no output)"), result.output


def test_nothing_is_interrupted_when_no_command_is_running(tool):
    assert tool.interrupt() is False


def test_the_model_is_sent_the_tail_when_the_human_saw_the_rest(tool, tmp_path):
    script = _script(tmp_path, "big.py", (
        "import sys\n"
        "for i in range(400):\n"
        "    sys.stdout.write(f'line {i:03d} ' + 'y' * 60 + chr(10))\n"
        "sys.stdout.flush()\n"
    ))
    seen = [0]
    tool.on_output = lambda kind, text: seen.__setitem__(0, seen[0] + 1)
    result = tool.execute(_command(script), timeout=BUDGET)

    assert seen[0] == 400, "the human did not see the whole transcript"
    assert "line 000 " not in result.output, "the whole transcript was pasted again"
    assert "line 399 " in result.output, "the model did not get the tail"
    assert "not repeated here" in result.output and "shown" in result.output
    assert len(result.output.splitlines()) <= LIVE_TAIL_LINES + 2


def test_a_short_streamed_result_is_still_sent_whole(tool, tmp_path):
    script = _script(tmp_path, "three.py", "import sys\n"
                                           "for i in (1, 2, 3): print(i, flush=True)\n")
    seen = []
    tool.on_output = lambda kind, text: seen.append(text)
    result = tool.execute(_command(script), timeout=BUDGET)
    assert seen == ["1", "2", "3"]
    assert result.output == "1\n2\n3\n", "a small result was clipped to be polite"


def test_a_stopped_tool_releases_its_run(tool, tmp_path):
    """The next `interrupt()` must answer False: a finished command is not stoppable."""
    script = _script(tmp_path, "finish.py", "print('done', flush=True)\n")
    tool.execute(_command(script), timeout=BUDGET)
    assert tool.interrupt() is False


# ----------------------------------------- the non-streaming path, byte for byte ---

def test_run_argv_without_a_callback_returns_what_it_always_did(tmp_path):
    """The same bytes `subprocess` itself would have handed back."""
    script = _script(tmp_path, "plain.py", (
        "import sys\n"
        "print('one')\n"
        "print('two', file=sys.stderr)\n"
    ))
    expected = subprocess.run([sys.executable, "-c",
                               "import sys; print('one'); print('two', file=sys.stderr)"],
                              capture_output=True)
    result = _run(script, timeout=BUDGET)
    assert isinstance(result, subprocess.CompletedProcess)
    assert result.stdout == expected.stdout
    assert result.stderr == expected.stderr
    assert result.returncode == 0
    assert result.interrupted is False
    assert not hasattr(result, "stopped_early")


def test_the_tool_result_text_is_the_one_the_model_already_reads(tool, tmp_path):
    """Pinned against the wording and layout in use before streaming existed."""
    quiet = _script(tmp_path, "quiet.py", "print('привет')\n")
    loud = _script(tmp_path, "loud.py",
                   "import sys\nprint('в')\nprint('ошибка', file=sys.stderr)\n")
    silent = _script(tmp_path, "silent.py", "pass\n")
    failing = _script(tmp_path, "failing.py", "import sys\nsys.exit(3)\n")

    assert run_text(_command(quiet)) == ("привет\n", "", 0)

    result = tool.execute(_command(loud), timeout=BUDGET)
    assert result.output == "в\n\n[stderr]\nошибка\n", repr(result.output)
    assert result.error is False and result.metadata["returncode"] == 0

    result = tool.execute(_command(silent), timeout=BUDGET)
    assert result.output == "(no output)" and result.error is False

    result = tool.execute(_command(failing), timeout=BUDGET)
    assert result.error is True and result.metadata["returncode"] == 3


def test_a_timeout_is_still_a_timeout_while_lines_are_flowing(tool, tmp_path):
    script = _script(tmp_path, "slow.py", (
        "import sys, time\n"
        "print('a line', flush=True)\n"
        "time.sleep(30)\n"
    ))
    seen = []
    tool.on_output = lambda kind, text: seen.append(text)
    result = tool.execute(_command(script), timeout=2)
    assert result.error is True
    assert "timed out after 2s" in result.output
    assert seen == ["a line"], "streaming did not survive into the timeout path"
    assert "shown live" in result.output


def test_a_callback_that_raises_does_not_break_the_command(tmp_path):
    script = _script(tmp_path, "nasty.py",
                     "import sys\nfor i in range(5):\n    print(i, flush=True)\n")
    calls = [0]

    def broken(kind, text):
        calls[0] += 1
        raise RuntimeError("the UI is gone")

    result = _run(script, timeout=BUDGET, on_line=broken)
    assert result.returncode == 0, "a dying callback took the command down with it"
    assert calls[0] == 1, "the reader kept shouting at a callback that already hung up"
    assert result.stdout.startswith(b"0"), "the capture was lost with the callback"


def test_a_sink_that_raises_inside_the_tool_is_not_the_model_s_error(tool, tmp_path):
    script = _script(tmp_path, "sink.py", "print('kept', flush=True)\n")

    def broken(kind, text):
        raise RuntimeError("the UI is gone")

    tool.on_output = broken
    result = tool.execute(_command(script), timeout=BUDGET)
    assert not result.error, result.output
    assert "kept" in result.output


def test_bash_still_refuses_to_be_called_safe(tool):
    assert tool.is_safe() is False


def test_a_watched_run_leaves_no_temp_capture_file(tmp_path, monkeypatch):
    """The redirect files are the design; surviving them is not.

    Measured in a temp root of this test's own, because the shared one does hold
    survivors and they belong to a different case: `shell.py:_discard` tries five
    times over ~0.1 s and then leaves a file a surviving grandchild still has
    open, which is the honest choice — deleting it out from under a process that
    is still writing would corrupt the run rather than tidy it. A snapshot of the
    global directory would blame this run for another test's leftovers, and on a
    box where two suites share the temp root that reads as a failure that is not.
    """
    private = tmp_path / "temp-root"
    private.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(private))
    script = _script(tmp_path, "litter.py", "print('x', flush=True)\n")
    _run(script, timeout=BUDGET, on_line=lambda kind, text: None, run=Run())
    left = sorted(p.name for p in private.iterdir())
    assert left == [], f"the run left its capture files behind: {left}"


def test_the_command_still_goes_through_the_real_shell():
    """`run_shell` keeps using `shell_command`, so && and ~ still mean what they did."""
    argv = shell_command("echo hi")
    assert argv[-1] == "echo hi"
    assert argv[0].lower().endswith(("bash", "bash.exe", "sh", "cmd.exe"))


def test_posix_chaining_still_streams(tool, tmp_path):
    if not _posix_shell():
        pytest.skip("no POSIX shell available")
    seen = []
    tool.on_output = lambda kind, text: seen.append(text)
    result = tool.execute("echo one && echo two && exit 0", timeout=BUDGET)
    assert not result.error, result.output
    assert seen == ["one", "two"]
