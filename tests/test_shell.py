"""Shell tool output: UTF-8 decoding and POSIX syntax on Windows."""
import pytest

from beeagent.tools.bash import BashTool
from beeagent.tools.git import GitTool
from beeagent.tools.shell import decode, run_text, shell_command


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


def test_run_text_returns_streams_separately():
    out, err, code = run_text(
        """python -c "import sys; print('в'); print('ошибка', file=sys.stderr)" """)
    assert code == 0, err
    assert out.strip() == "в" and "ошибка" in err


def test_git_tool_runs_without_repo_and_decodes():
    result = GitTool().execute("--version")
    assert not result.error
    assert result.output.startswith("git version")
