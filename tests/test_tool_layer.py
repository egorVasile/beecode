"""The file tools, driven with the hostile input the audit used.

Two rules shape this file:

* the tools run for real, in a directory that is the process cwd, and the
  assertion is about the BYTES LEFT ON DISK — not about a wording the tool
  happened to print;
* "outside" means outside every root the policy carries, so the escape target
  here is a sibling of the interpreter's temp directory rather than a neighbour
  of `tmp_path`: the temp tree is a root BeeCode is allowed to write into
  (scratch files, and every fixture here), so a neighbour of `tmp_path` proves
  nothing.

Everything under `tmp_path` is the legit case: a project the agent was started
inside, with Russian file names and Russian text, because that is the normal
input for the users of this program.
"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.tools import _path_policy
from beeagent.tools import edit as edit_mod
from beeagent.tools import grep as grep_mod
from beeagent.tools.edit import EditTool
from beeagent.tools.glob_tool import GlobTool
from beeagent.tools.grep import GrepTool
from beeagent.tools.list_dir import ListDirectoryTool
from beeagent.tools.read import ReadTool
from beeagent.tools.write import WriteTool

VICTIM = b"ssh-rsa AAAA-the-users-own-key\n"


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def project(tmp_path, monkeypatch):
    """A working folder the agent was started inside, with Russian content."""
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "платежи.py").write_bytes(
        "# обработчик платежей\ndef platezh():\n    return 'оплата'\n".encode("cp1251"))
    (tmp_path / "src" / "orders.py").write_text("def charge_order():\n    pass\n", encoding="utf-8")
    (tmp_path / "tests").mkdir(exist_ok=True)
    (tmp_path / "tests" / "t_orders.py").write_text("def charge():\n    pass\n", encoding="utf-8")
    (tmp_path / "заметки.md").write_text("# проект\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def outside(tmp_path):
    """A real directory, writable, that no root of the policy covers."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    candidate = temp_root.parent / f"beecode-outside-{os.getpid()}-{tmp_path.name}"
    import shutil

    try:
        candidate.mkdir(parents=True)
    except OSError:              # a box whose temp directory has no writable parent
        pytest.skip(f"cannot create an escape directory beside {temp_root}")
    (candidate / "authorized_keys").write_bytes(VICTIM)
    yield candidate
    shutil.rmtree(candidate, ignore_errors=True)


def refused(result, *needles):
    """A refusal: `error=True`, never dressed as a result, naming what it means."""
    assert result.error is True, f"a refusal that reports success: {result.output[:120]!r}"
    for claim in ("Written ", "Replaced in", "Изменено в", "Записано"):
        assert claim not in result.output, result.output[:120]
    for needle in needles:
        assert needle in result.output, result.output[:220]
    return result


def make_escape_link(directory: Path, target: Path, name: str) -> bool:
    """Point `directory/name` at `target` with whatever link this box allows."""
    link = directory / name
    try:
        os.symlink(str(target), str(link))
        return link.is_symlink() or link.is_dir()
    except OSError:
        pass
    if os.name == "nt" and target.is_dir():
        # A junction is not a symlink: no privilege needed, and realpath follows it.
        done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                              capture_output=True)
        return done.returncode == 0 and link.exists()
    return False


# ---------------------------------------------------------------------------
# 1 — working-directory containment, in all six path-taking tools
# ---------------------------------------------------------------------------

def test_write_refuses_an_absolute_path_outside_and_the_bytes_stay(project, outside):
    victim = outside / "authorized_keys"
    result = WriteTool().execute(path=str(victim), content="ssh-rsa AAAA attacker-key\n")
    refused(result, "OUTSIDE the working directory", str(victim))
    assert victim.read_bytes() == VICTIM, "the file the audit proved was overwritten"
    assert not list(outside.glob("*.beecode-tmp")), "no temp file left beside it either"


def test_read_edit_grep_glob_and_list_all_refuse_the_same_path(project, outside):
    victim = outside / "authorized_keys"
    refused(ReadTool().execute(path=str(victim)), "OUTSIDE the working directory")
    refused(EditTool().execute(path=str(victim), old_text="ssh-rsa", new_text="x"),
            "OUTSIDE the working directory")
    refused(GrepTool().execute(pattern="ssh", path=str(outside)), "OUTSIDE the working directory")
    refused(GlobTool().execute(pattern="**/*", path=str(outside)), "OUTSIDE the working directory")
    refused(ListDirectoryTool().execute(str(outside)), "OUTSIDE the working directory")
    assert victim.read_bytes() == VICTIM


def test_traversal_refuses_without_creating_the_directory_it_names(project):
    """`../../x` used to build folders on its way to writing the file."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    target = temp_root.parent / f"beecode-escaped-{os.getpid()}"
    path = os.path.relpath(str(target / "pwned.txt"), os.getcwd()).replace(os.sep, "/")
    assert path.startswith(".."), f"not an escape after all: {path}"
    result = WriteTool().execute(path=path, content="hi")
    refused(result, "OUTSIDE the working directory")
    assert not target.exists(), "a directory was created outside the project"


def test_tilde_is_expanded_and_then_refused(project, outside, monkeypatch):
    monkeypatch.setenv("HOME", str(outside))
    monkeypatch.setenv("USERPROFILE", str(outside))
    result = WriteTool().execute(path="~/authorized_keys", content="ssh-rsa AAAA attacker\n")
    refused(result, "OUTSIDE the working directory")
    assert (outside / "authorized_keys").read_bytes() == VICTIM
    refused(WriteTool().execute(path="~/no-such-folder/x.txt", content="x"))
    assert not (outside / "no-such-folder").exists(), "expanduser still built a tree outside"


def test_a_directory_link_out_is_outside_even_though_it_sits_inside(project, outside):
    """The lexical check is beaten by a link; realpath is not."""
    if not make_escape_link(project, outside, "elsewhere"):
        pytest.skip("this box allows neither symlinks nor junctions")
    target = "elsewhere/authorized_keys"
    refused(ReadTool().execute(path=target), "OUTSIDE the working directory")
    refused(WriteTool().execute(path=target, content="overwrite\n"),
            "OUTSIDE the working directory")
    refused(EditTool().execute(path=target, old_text="ssh-rsa", new_text="x"),
            "OUTSIDE the working directory")
    assert (outside / "authorized_keys").read_bytes() == VICTIM
    assert (project / "elsewhere").is_symlink() or (project / "elsewhere").is_dir(), \
        "the refusal destroyed the link instead of leaving it alone"


def test_a_file_link_out_is_not_read_through_by_grep_or_glob(project, outside):
    if not make_escape_link(project, outside / "authorized_keys", "keys.link"):
        pytest.skip("this box allows no symlinks")
    result = GrepTool().execute(pattern="ssh-rsa", path=".")
    assert "keys.link" not in result.output, result.output[:200]
    assert "outside the working directory" in result.output, result.output[:200]
    globbed = GlobTool().execute(pattern="*.link", path=".")
    assert "keys.link" not in globbed.output, globbed.output[:200]
    refused(ReadTool().execute(path="keys.link"), "OUTSIDE the working directory")
    assert (outside / "authorized_keys").read_bytes() == VICTIM


@pytest.mark.skipif(os.name != "nt", reason="alternate data streams are an NTFS feature")
def test_an_alternate_data_stream_name_is_refused_and_no_stream_appears(project):
    readme = project / "readme.md"
    readme.write_text("# project\n", encoding="utf-8")
    refused(WriteTool().execute(path="readme.md:evil.svg", content="<svg/>\n"),
            "not an ordinary file name")
    with pytest.raises(OSError):
        open(str(readme) + ":evil.svg", "rb").read()
    assert readme.read_text(encoding="utf-8") == "# project\n"


@pytest.mark.skipif(os.name != "nt", reason="device names are a Windows concept")
def test_windows_names_that_mean_something_else_are_refused(project):
    """`NUL`, `con.py`, a trailing dot and `C:name` all land somewhere else."""
    for raw in ("NUL", "con.py", "файл.txt.", "C:driveskip.txt"):
        refused(WriteTool().execute(path=raw, content="x"), "Refused")
    # `Path("NUL").exists()` is True on every Windows box, device or not, so the
    # honest check is what a real file would have been called.
    for written in ("con.py", "файл.txt", "driveskip.txt"):
        assert not (project / written).exists(), f"{written} was created anyway"
    assert sorted(p.name for p in project.iterdir() if p.is_file()) == ["заметки.md"]


def test_a_path_the_user_confirmed_is_allowed_because_this_is_a_policy(project, outside,
                                                                       monkeypatch):
    """Refusing forever would break the case where the user named the folder."""
    monkeypatch.setenv("BEECODE_TRUSTED_DIRS", str(outside))
    victim = outside / "authorized_keys"
    result = WriteTool().execute(path=str(victim), content="the user asked for this one\n")
    assert result.error is False, result.output
    assert victim.read_text(encoding="utf-8") == "the user asked for this one\n"
    monkeypatch.delenv("BEECODE_TRUSTED_DIRS")
    refused(WriteTool().execute(path=str(victim), content="back to refused\n"))
    assert victim.read_text(encoding="utf-8") == "the user asked for this one\n"


def test_grant_root_is_the_other_way_a_human_opens_a_folder(project, outside):
    """The hook a confirmation prompt would call, once the user has said yes."""
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.delenv(_path_policy.TRUSTED_DIRS_ENV, raising=False)
    refused(ReadTool().execute(path=str(outside / "authorized_keys")),
            "OUTSIDE the working directory")
    _path_policy.grant_root(outside)
    try:
        read = ReadTool().execute(path=str(outside / "authorized_keys"))
        assert read.error is False, read.output
        assert "the-users-own-key" in read.output
    finally:
        _path_policy.forget_granted_roots()
        monkeypatch.undo()
    refused(ReadTool().execute(path=str(outside / "authorized_keys")),
            "OUTSIDE the working directory")


def test_inside_the_project_still_works_exactly_as_before(project):
    """The legit path: a Russian file name, a new folder, an edit, a listing."""
    assert WriteTool().execute(path="src/новый_модуль.py",
                               content="оплата = 1\n").error is False
    assert (project / "src" / "новый_модуль.py").read_bytes() == "оплата = 1\n".encode("utf-8")
    read = ReadTool().execute(path="заметки.md")
    assert read.error is False and "1: # проект" in read.output
    assert EditTool().execute(path="src/orders.py", old_text="    pass",
                              new_text="    return 1").error is False
    assert "    return 1" in (project / "src" / "orders.py").read_text(encoding="utf-8")
    listed = ListDirectoryTool().execute(".")
    assert listed.error is False and "заметки.md" in listed.output and "src/" in listed.output
    globbed = GlobTool().execute(pattern="**/*.py", path=".")
    assert "src/orders.py" in globbed.output and "платежи.py" in globbed.output
    assert GrepTool().execute(pattern="def ", path=".").error is False


# ---------------------------------------------------------------------------
# 2 — grep decoded the wrong file and lied about it
# ---------------------------------------------------------------------------

def test_grep_finds_russian_text_in_a_cp1251_file(project):
    """cp1251 bytes read with errors="replace" answered "No matches found"."""
    for pattern in ("платеж", "оплата", "обработчик"):
        result = GrepTool().execute(pattern=pattern, path="src")
        assert result.error is False, f"{pattern!r}: {result.output!r}"
        assert "платежи.py" in result.output, f"{pattern!r} is in that file: {result.output!r}"
    assert "not UTF-8" in GrepTool().execute(pattern="платеж", path="src").output, \
        "the answer must say which encoding it had to use"


def test_grep_says_what_it_did_not_search_instead_of_no_matches(project):
    (project / "junk.bin").write_bytes(bytes(range(1, 256)) * 4)     # no NUL, undecodable
    (project / "blob.dat").write_bytes(b"\x00\x01ERROR\x02" * 100)   # binary
    result = GrepTool().execute(pattern="zzzqqq", path=".")
    out = result.output
    assert "No matches found" in out
    assert "No matches found\n" not in out, f"an unqualified lie: {out!r}"
    assert "NOT searched" in out and "binary" in out and "encoding" in out
    assert "6 files" in out or "file" in out, out


def test_include_pattern_behaves_the_way_a_user_types_it(project):
    """`src/*.py` matched nothing: fnmatch ran against a bare name and a full path."""
    for include in ("src/*.py", "src/**/*.py", "**/src/*.py", "src\\*.py", "*.py", "*.py ",
                    "orders.py", "[so]*.py"):
        result = GrepTool().execute(pattern="def ", path=".", include=include)
        assert result.error is False, f"{include!r}: {result.output!r}"
        assert "orders.py" in result.output, f"{include!r} matched nothing: {result.output!r}"
    only_src = GrepTool().execute(pattern="def ", path=".", include="src/*.py").output
    assert "t_orders.py" not in only_src, "the folder part of the pattern was ignored"
    wrong = GrepTool().execute(pattern="def ", path=".", include="docs/*.py")
    refused(wrong, "matched none of the", "src/*.py")
    refused(GrepTool().execute(pattern="def ", path=".", include="/src/*.py"),
            "relative to the search root")
    refused(GrepTool().execute(pattern="def ", path=".", include="../src/*.py"),
            "relative to the search root")


def test_a_cp1251_russian_file_is_refused_in_words_and_its_bytes_survive(project):
    """`read` showing U+FFFD as content is how a cp1251 file got rewritten."""
    target = project / "src" / "платежи.py"
    before = target.read_bytes()
    result = ReadTool().execute(path="src/платежи.py")
    assert result.error is True
    assert "UTF-8" in result.output and "\ufffd" not in result.output
    assert target.read_bytes() == before, "a refused read still touched the file"
    assert GrepTool().execute(pattern="платеж", path="src").error is False


def test_grep_searches_a_declared_encoding_and_says_which(project):
    (project / "koi8.py").write_bytes("# -*- coding: koi8-r -*-\nплатеж = 1\n".encode("koi8-r"))
    result = GrepTool().execute(pattern="платеж", path="koi8.py")
    assert result.error is False, result.output
    assert "koi8" in result.output, result.output


# ---------------------------------------------------------------------------
# 3 — the 4,000,041-character answer
# ---------------------------------------------------------------------------

def test_grep_of_a_multi_megabyte_single_line_is_capped_and_says_so(project):
    (project / "blob.bin").write_bytes(b"\x00\x01ERROR\x02" * 500000)        # 3.5 MB, binary
    result = GrepTool().execute(pattern="ERROR", path="blob.bin")
    assert len(result.output) < 4000, f"{len(result.output)} chars came back"
    assert "NOT searched" in result.output and "bigger than" in result.output

    (project / "small_binary.bin").write_bytes(b"\x00\x01ERROR\x02" * 100)
    binary = GrepTool().execute(pattern="ERROR", path="small_binary.bin")
    assert "binary" in binary.output and "NOT searched" in binary.output, binary.output
    assert len(binary.output) < 1000

    (project / "one_line.log").write_bytes(b"ERROR " + b"x" * 3_000_000)     # 3 MB, one text line
    single = GrepTool().execute(pattern="ERROR", path="one_line.log")
    assert len(single.output) < 1500, f"{len(single.output)} chars for one line"
    assert "NOT searched" in single.output, single.output[-160:]

    (project / "many_lines.log").write_bytes(b"ERROR line\n" * 40000)        # 40k matches
    many = GrepTool().execute(pattern="ERROR", path="many_lines.log")
    assert len(many.output) <= grep_mod.MAX_OUTPUT_CHARS, len(many.output)
    assert len(many.output.splitlines()) <= grep_mod.MAX_MATCHES + 5, "every match was pasted in"
    assert "more matches not shown" in many.output, many.output[-200:]
    assert many.metadata["count"] == 40000, many.metadata


# ---------------------------------------------------------------------------
# 4 — read: MAX_LINE_CHARS had no callers, and EOF was silent
# ---------------------------------------------------------------------------

def test_read_does_not_return_a_two_megabyte_line_whole(project):
    (project / "one_line.js").write_text("var x = 1;" * 200000, encoding="utf-8")
    result = ReadTool().execute(path="one_line.js")
    assert result.error is False
    assert len(result.output) < 3000, f"{len(result.output)} characters for one line"
    assert "2000" in result.output, "the answer must say what the limit was"
    assert "…" in result.output


def test_read_past_the_end_of_the_file_is_an_error(project):
    (project / "short.txt").write_text("a\nb\nc\n", encoding="utf-8")
    past = ReadTool().execute(path="short.txt", offset=500)
    assert past.error is True, f"'' with error=False reads as an empty tail: {past.output!r}"
    assert "past the end" in past.output and "3" in past.output
    assert ReadTool().execute(path="short.txt", offset=2).output.startswith("3: c")


def test_read_still_clamps_the_lines_and_names_the_next_offset(project):
    (project / "many.txt").write_text(("строка" + "\n") * 50000, encoding="utf-8")
    result = ReadTool().execute(path="many.txt")
    assert len(result.output.splitlines()) <= 2001, "the whole file came back"
    assert "48000 more lines" in result.output.splitlines()[-1]
    assert "offset=2000" in result.output


# ---------------------------------------------------------------------------
# 5 — the fabricated count
# ---------------------------------------------------------------------------

def test_list_directory_counts_what_it_did_not_show(project):
    many = project / "many"
    many.mkdir()
    for index in range(350):
        (many / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
    result = ListDirectoryTool().execute("many")
    lines = result.output.splitlines()
    shown = sum(1 for line in lines[1:] if line.startswith("f"))
    assert shown == 300, shown
    assert "showing 300 of 350 entries" in lines[0], lines[0]
    assert lines[-1] == "… and 50 more not shown", lines[-1]
    assert result.metadata["count"] == 350, result.metadata


def test_list_directory_of_four_hundred_russian_names_is_honest(project):
    many = project / "много"
    many.mkdir()
    for index in range(400):
        (many / f"файл_{index:03d}.txt").write_text("содержимое", encoding="utf-8")
    result = ListDirectoryTool().execute("много")
    assert result.error is False
    assert "файл_000.txt" in result.output
    assert "showing 300 of 400 entries" in result.output.splitlines()[0], result.output[:80]
    assert "and 100 more not shown" in result.output


# ---------------------------------------------------------------------------
# 6 — the staleness stamp: when it is taken, and what it is keyed by
# ---------------------------------------------------------------------------

def test_edit_refuses_a_save_that_landed_during_its_own_read(project, monkeypatch):
    """The stamp used to be taken after the read, so the race won and said "Replaced"."""
    target = project / "app.py"
    target.write_text("line1\nline2\nline3\n", encoding="utf-8")
    assert ReadTool().execute(path="app.py").error is False
    real_read = edit_mod.read_text_preserving

    def read_then_someone_saves(path):
        text = real_read(path)
        Path(path).write_text("line1\nTHE USER'S OWN LINE\nline3\n", encoding="utf-8")
        return text

    monkeypatch.setattr(edit_mod, "read_text_preserving", read_then_someone_saves)
    result = EditTool().execute(path="app.py", old_text="line2", new_text="AGENT LINE")
    on_disk = target.read_text(encoding="utf-8")
    assert result.error is True, "the tool reported a clean replacement"
    assert "changed" in result.output
    assert "THE USER'S OWN LINE" in on_disk, "the user's work was dropped"
    assert "AGENT LINE" not in on_disk


def test_a_read_by_absolute_path_protects_an_edit_by_relative_one(project):
    """The stamps were keyed by the literal spelling, so the check never fired."""
    target = project / "src" / "orders.py"
    assert ReadTool().execute(path=str(target)).error is False
    target.write_text("def charge_order():\n    # the user edited this\n    pass\n",
                      encoding="utf-8")
    for spelling in ("src/orders.py", "./src/orders.py", str(target)):
        refused(EditTool().execute(path=spelling, old_text="    pass", new_text="    return 1"),
                "changed since you read it")
    assert "# the user edited this" in target.read_text(encoding="utf-8")
    assert "    return 1" not in target.read_text(encoding="utf-8")


def test_the_happy_path_still_edits_and_keeps_the_files_own_line_endings(project):
    crlf = project / "win.py"
    crlf.write_bytes("a = 1\r\nb = 2\r\n".encode("utf-8"))
    assert ReadTool().execute(path="win.py").error is False
    assert EditTool().execute(path="win.py", old_text="a = 1",
                              new_text="оплата = 1").error is False
    assert crlf.read_bytes() == "оплата = 1\r\nb = 2\r\n".encode("utf-8")
    assert EditTool().execute(path="win.py", old_text="b = 2", new_text="c = 3").error is False
    assert crlf.read_bytes() == "оплата = 1\r\nc = 3\r\n".encode("utf-8")


# ---------------------------------------------------------------------------
# 7 — a valid UTF-8 file refused as "not UTF-8"
# ---------------------------------------------------------------------------

def test_a_multibyte_character_split_across_the_probe_boundary_is_still_text(project):
    target = project / "valid_utf8.txt"
    target.write_bytes(b"a" * 65535 + "я".encode("utf-8") + b"b" * 100)
    target.read_text(encoding="utf-8")                       # it is valid, by definition
    result = WriteTool().execute(path="valid_utf8.txt", content="новое содержимое\n")
    assert result.error is False, result.output
    assert target.read_bytes() == "новое содержимое\n".encode("utf-8")


def test_write_reports_bytes_and_not_characters(project):
    target = project / "cyr.txt"
    result = WriteTool().execute(path="cyr.txt", content="Оплата прошла")   # 15 chars, 25 bytes
    assert "25 bytes" in result.output, result.output
    assert target.stat().st_size == 25


def test_write_still_refuses_a_file_that_is_genuinely_not_utf8(project):
    target = project / "utf16.ps1"
    target.write_bytes("Hello".encode("utf-16"))
    refused(WriteTool().execute(path="utf16.ps1", content="Hello"), "UTF-16")
    assert target.read_bytes().startswith(b"\xff\xfe")
    (project / "binary.ico").write_bytes(b"\x00\x01\x02\xff\xfe\xfd" * 10)
    refused(WriteTool().execute(path="binary.ico", content="x"), "not UTF-8")
    assert target.read_bytes().startswith(b"\xff\xfe")


# ---------------------------------------------------------------------------
# the refusal text itself: two languages, and it names the target
# ---------------------------------------------------------------------------

def test_the_refusal_is_bilingual_and_names_the_resolved_target(project, outside):
    victim = outside / "authorized_keys"
    english = WriteTool().execute(path=str(victim), content="x").output
    assert "refused" in english and str(victim) in english
    i18n.set_lang("ru")
    try:
        russian = WriteTool().execute(path=str(victim), content="x").output
    finally:
        i18n.set_lang("en")
    assert "отказал" in russian and str(victim) in russian
    assert "Записано" not in russian


def test_no_tool_invents_a_working_directory_of_its_own(project):
    """The policy is the process cwd, which is where the agent was started."""
    assert _path_policy.guard(str(project / "src"))[1] == ""
    assert _path_policy.working_dir() == _path_policy._norm(os.path.realpath(os.getcwd()))
