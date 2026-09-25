"""Shell tool output: UTF-8 decoding and POSIX syntax on Windows."""
import time

import pytest

from beeagent.tools.bash import BashTool
from beeagent.tools.git import GitTool
from beeagent.tools.shell import decode, run_argv_text, run_text, shell_command


def _has_posix_shell():
    return shell_command("true")[0].lower().endswith(("bash", "bash.exe", "sh"))


def test_decode_reads_utf8():
    assert decode("привет пчела".encode("utf-8")) == "привет пчела"


def test_decode_survives_legacy_codepage_bytes():
    # A cp866-speaking child must not crash the tool; bytes become text either way.
    legacy = "привет".encode("cp866")
    text = decode(legacy)
    assert isinstance(text, str) and text


def test_decode_normalizes_windows_newlines():
    assert decode(b"one\r\ntwo\r\n") == "one\ntwo\n"


def test_shell_command_prefers_a_posix_shell():
    argv = shell_command("echo hi")
    assert argv[-1] == "echo hi"
    assert argv[0].lower().endswith(("bash", "bash.exe", "sh", "cmd.exe"))


def test_bash_tool_decodes_cyrillic_output():
    # "python" resolves the same way under git-bash and cmd.exe.
    result = BashTool().execute("""python -c "print('привет')" """)
    assert not result.error, result.output
    assert result.output.strip() == "привет"


def test_bash_tool_supports_posix_chaining():
    if not shell_command("true")[0].lower().endswith(("bash", "bash.exe", "sh")):
        pytest.skip("no POSIX shell available")
    result = BashTool().execute(
        'tmp=$(mktemp -d) && cd "$tmp" && echo ок > note.txt '
        '&& cat note.txt && pwd && rm -rf "$tmp"')
    assert not result.error, result.output
    assert "ок" in result.output


def test_bash_tool_reports_failure():
    result = BashTool().execute("exit 3")
    assert result.error and result.metadata["returncode"] == 3


def test_bash_tool_times_out():
    result = BashTool().execute("sleep 5", timeout=1)
    assert result.error and "timed out" in result.output


def test_bash_timeout_kills_the_whole_process_tree(tmp_path):
    """Pinned: "timed out" must mean the program stopped, not that we stopped waiting.

    `subprocess.run(timeout=…)` kills only the direct child and then waits on the
    pipes the grandchildren still hold, which is how a one-second timeout came
    back after four and left the model's `cargo build` running — and writing
    files — for another minute. The audit measured the tree really dying here;
    this is the test that keeps it that way.
    """
    if not _has_posix_shell():
        pytest.skip("needs a POSIX shell to background a child")
    log = tmp_path / "grandchild.log"
    ticker = tmp_path / "tick.py"
    ticker.write_text("import sys, time\n"
                      "while True:\n"
                      "    with open(sys.argv[1], 'a') as fh:\n"
                      "        fh.write('tick\\n')\n"
                      "    time.sleep(0.1)\n", encoding="utf-8")
    command = f"python '{ticker.as_posix()}' '{log.as_posix()}' & sleep 30"

    started = time.time()
    result = BashTool().execute(command, timeout=2)
    assert result.error and "timed out" in result.output
    assert time.time() - started < 12, "the wait was held open by a survivor"

    first = log.read_text(encoding="utf-8").count("tick")
    assert first >= 2, f"the grandchild never ran ({first} lines): the test proves nothing"
    time.sleep(3)
    assert log.read_text(encoding="utf-8").count("tick") == first, \
        "an orphan kept writing after the timeout"


def test_a_backgrounded_child_holding_the_redirect_does_not_freeze_the_tool(tmp_path):
    """The redirect is a file, so no pipe is left for a survivor to hold open."""
    if not _has_posix_shell():
        pytest.skip("needs a POSIX shell to background a child")
    started = time.time()
    result = BashTool().execute("sleep 30 & sleep 30", timeout=2)
    took = time.time() - started
    assert result.error and "timed out" in result.output
    assert took < 10, f"the tool waited {took:.1f}s on a child it had killed"


def test_run_argv_env_reaches_the_child():
    """What `git` relies on to promise `--no-pager` without a shell of its own."""
    code = "import os; print(os.environ.get('BEECODE_PROBE'))"
    out, err, code_ = run_argv_text(["python", "-c", code],
                                    env={"BEECODE_PROBE": "layered over os.environ"})
    assert code_ == 0, err
    assert out.strip() == "layered over os.environ"


def test_run_argv_env_cannot_be_beaten_by_the_inherited_one(monkeypatch):
    monkeypatch.setenv("BEECODE_PROBE", "inherited")
    code = "import os; print(os.environ.get('BEECODE_PROBE'))"
    out, err, returncode = run_argv_text(["python", "-c", code],
                                         env={"BEECODE_PROBE": "tool's own"})
    assert returncode == 0, err
    assert out.strip() == "tool's own"


def test_run_text_returns_streams_separately():
    out, err, code = run_text(
        """python -c "import sys; print('в'); print('ошибка', file=sys.stderr)" """)
    assert code == 0, err
    assert out.strip() == "в" and "ошибка" in err


def test_git_tool_runs_without_repo_and_decodes():
    result = GitTool().execute("--version")
    assert not result.error
    assert result.output.startswith("git version")


# --- the capped capture, and what another thread may ask of a live command ------

def test_capped_text_without_a_tail_is_the_capture_exactly_as_it_was():
    """The default of `capped_text` is the whole text, note included."""
    from beeagent.tools.shell import MAX_CAPTURE, capped_text

    data = b"one\ntwo\n"
    assert capped_text(data) == "one\ntwo\n" and capped_text(data, 1) == capped_text(data)
    oversized = b"x" * (MAX_CAPTURE + 10)
    text = capped_text(oversized)
    assert len(text) < len(oversized) and "output cut at" in text


def test_capped_text_holds_back_the_lines_the_user_already_saw():
    """Streaming: the model gets the tail plus the count, not a second transcript."""
    from beeagent.tools.shell import capped_text

    body = "".join(f"line {i}\n" for i in range(100)).encode()
    text = capped_text(body, keep_last=5)
    assert "line 99" in text and "line 0\n" not in text
    assert "95 earlier line(s)" in text and "shown as the command printed them" in text
    assert len(text.splitlines()) == 6          # five lines and the sentence


def test_a_run_can_be_asked_to_stop_from_another_thread():
    """`Run` is the whole surface between the UI thread and a live command."""
    import threading

    from beeagent.tools.shell import Run

    run = Run()
    assert run.running is False and run.pid is None
    results = []
    threads = [threading.Thread(target=lambda: results.append(run.interrupt()))
               for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(results) == 1, f"{results}: more than one thread was told it stopped it"
    assert run.stop_requested is True
