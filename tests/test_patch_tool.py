"""`patch` — one diff, many files, and no lying about what landed.

The tool exists because a refactor across five files otherwise costs five `edit`
calls, each needing its own exact string, and the fourth usually fails after the
first three already changed the tree. So every test here is about the two things
that make a patch tool safe to hand to a free model:

* **bytes, not words.** The assertion is what the file holds after the call, read
  back from disk, in the file's own line endings, and the byte count the report
  prints is compared against `stat()` rather than trusted.
* **all of it or none of it.** The strongest test here is
  `test_a_half_applying_diff_leaves_every_file_byte_identical`: a three-file diff
  whose third file cannot apply has to leave all three files exactly as they were,
  byte for byte.

`beeagent/core/journal.py` is another agent's work and may or may not exist, so
the journal is always replaced: `_journal` installs a fake whose `record()` logs
the bytes the file held at that moment - which is how "logged before written" is
proven - and the journal tests take it apart again.

The console on this box is cp1251, so nothing here prints Russian: the Russian
strings are asserted through UTF-8 files written under tmp_path.
"""
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.tools import _seen, patch as patch_mod
from beeagent.tools.patch import PatchTool

VICTIM = b"ssh-rsa AAAA-the-users-own-key\n"

# One blank line between the definitions, and every hunk header below counts from
# it: `def one():` is line 3 and `def two():` is line 6, which is what `@@ -3,2`
# and `@@ -6,2` declare. With the usual two blank lines each hunk would land one
# and two lines later than declared, so half the tests here would be asserting an
# offset the diff never meant to have.
GOOD_FILE = """import os

def one():
    return 1

def two():
    return 2
"""


# ------------------------------------------------------------------ helpers ---

def d(*lines) -> str:
    """The diff text, built line by line, as a model would send it."""
    return "\n".join(lines) + "\n"


def crlf(*lines) -> str:
    """The same diff written on Windows: CRLF between its own lines."""
    return "\r\n".join(lines) + "\r\n"


def snapshot(root: Path) -> dict:
    """relpath -> bytes for every file under `root`."""
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()}


def size(root: Path, rel: str) -> int:
    return (root / rel).stat().st_size


def _write_lf(path: Path, text: str) -> None:
    """The bytes of `text`, with the newlines it was written with: no platform
    translation, so an LF fixture stays LF on disk on every OS."""
    path.write_bytes(text.encode("utf-8"))


class FakeJournal:
    """The journal API the tool expects, plus a log of what the disk held then."""

    def __init__(self):
        self.calls = []
        self.boom = None          # an Exception `record()` should raise
        self.returns = True       # False makes record() report "not stored"

    def record(self, workdir, tool, path, action="modify"):
        if self.boom:
            raise self.boom
        self.calls.append({"workdir": workdir, "tool": tool, "path": path, "action": action,
                           "bytes_at_record": _read_or_none(Path(path))})
        return self.returns

    @property
    def actions(self):
        return {os.path.basename(str(Path(c["path"]))): c["action"] for c in self.calls}

    @property
    def paths(self):
        return [c["path"] for c in self.calls]


def _read_or_none(path: Path):
    try:
        return path.read_bytes()
    except OSError:
        return None


def refused(result, *needles):
    """A refusal: `error=True`, no success wording anywhere, and it says what it means."""
    assert result.error is True, f"a refusal that reports success: {result.output[:200]!r}"
    for claim in ("patch applied", "bytes written", "патч применён"):
        assert claim not in result.output, result.output[:200]
    for needle in needles:
        assert needle in result.output, result.output[:400]
    return result


def make_escape_link(directory: Path, target: Path, name: str) -> bool:
    link = directory / name
    try:
        os.symlink(str(target), str(link))
        return link.is_symlink()
    except OSError:
        return False


# ----------------------------------------------------------------- fixtures ---

@pytest.fixture
def project(tmp_path, monkeypatch):
    """A folder the agent was started inside, with the files these tests patch.

    `write_bytes` rather than `write_text`: on Windows `Path.write_text` runs the
    text through the platform line translation, so a fixture written from a
    LF-only string would land on disk with CRLF and every byte-for-byte
    assertion below would be comparing against endings this test never asked for.
    """
    (tmp_path / "src").mkdir()
    _write_lf(tmp_path / "src" / "one.py", GOOD_FILE)
    _write_lf(tmp_path / "src" / "two.py", "alpha = 1\nbeta = 2\n")
    _write_lf(tmp_path / "src" / "three.py", "def drop():\n    pass\n")
    monkeypatch.chdir(tmp_path)
    _seen.STAMPS.clear()          # the staleness table is process-global
    return tmp_path


@pytest.fixture
def journal(monkeypatch, project):
    """Stand in for `beeagent.core.journal`, however far ahead of us it is."""
    fake = FakeJournal()
    module = types.ModuleType("beeagent.core.journal")
    module.record = fake.record
    import beeagent.core as core_package

    monkeypatch.setattr(core_package, "journal", module, raising=False)
    monkeypatch.setitem(sys.modules, "beeagent.core.journal", module)
    return fake


@pytest.fixture
def outside(project):
    """A real, writable directory that no root of the path policy covers.

    A neighbour of `tmp_path` proves nothing: the temp tree is itself a root
    BeeCode may write into, and that is where this project's fixtures live.
    """
    temp_root = Path(tempfile.gettempdir()).resolve()
    candidate = temp_root.parent / f"beecode-patch-outside-{os.getpid()}-{project.name}"
    try:
        candidate.mkdir(parents=True)
    except OSError:              # a box whose temp directory has no writable parent
        pytest.skip(f"cannot create an escape directory beside {temp_root}")
    (candidate / "authorized_keys").write_bytes(VICTIM)
    yield candidate
    shutil.rmtree(str(candidate), ignore_errors=True)


# ----------------------------------------------------------------- the tool ---

def test_it_advertises_itself_as_a_writing_unsafe_tool():
    tool = PatchTool()
    assert tool.name == "patch"
    assert tool.writes_files is True
    assert tool.is_safe() is False, "patch changes the machine; it is not safe to look with"
    schema = tool.to_schema()
    assert schema["parameters"]["required"] == ["diff"]
    assert set(schema["parameters"]["properties"]) == {"diff", "root"}
    assert "/allow patch" in tool.description
    assert tool.missing_args({}) == ["diff"]


def test_a_bad_call_is_answered_and_the_tree_is_untouched(project, journal):
    before = snapshot(project)
    for bad in ("", None, 42, ["--- a/x"]):
        refused(PatchTool().execute(diff=bad), "diff")
    assert snapshot(project) == before
    assert journal.calls == []


# ------------------------------------------------------------ many in one call ---

def test_three_files_applied_in_one_call(project, journal):
    """The whole point: one patch where `edit` would need six calls."""
    result = PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 1", "+    return 11",
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -1,2 +1,3 @@", "-alpha = 1", "-beta = 2", "+alpha = 10", "+beta = 20", "+gamma = 30",
        "--- /dev/null", "+++ b/src/four.py",
        "@@ -0,0 +1,2 @@", "+from src.one import one", "+VALUE = one()",
    ))
    assert not result.error, result.output
    assert (project / "src" / "one.py").read_text(encoding="utf-8") == GOOD_FILE.replace(
        "    return 1", "    return 11")
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 10\nbeta = 20\ngamma = 30\n"
    assert (project / "src" / "four.py").read_bytes() == b"from src.one import one\nVALUE = one()\n"

    # Every byte count in the report is the number the file really has on disk.
    one, two, four = (size(project, "src/one.py"), size(project, "src/two.py"),
                      size(project, "src/four.py"))
    assert "  src/one.py: modified, 1 hunk(s), %d bytes written" % one in result.output
    assert "  src/two.py: modified, 1 hunk(s), %d bytes written" % two in result.output
    assert "  src/four.py: created, 1 hunk(s), %d bytes written" % four in result.output
    assert result.output.rstrip().endswith(
        "patch applied: 3 file(s), 3 hunk(s), %d bytes written" % (one + two + four))
    assert result.metadata["bytes"] == one + two + four
    assert [f["hunks"] for f in result.metadata["files"]] == [1, 1, 1]
    assert journal.actions == {"one.py": "modify", "two.py": "modify", "four.py": "new"}


def test_two_hunks_of_one_file_are_both_reported(project, journal):
    result = PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 1", "+    return 11",
        "@@ -6,2 +6,2 @@", " def two():", "-    return 2", "+    return 22",
    ))
    assert not result.error, result.output
    assert (project / "src" / "one.py").read_bytes() == GOOD_FILE.replace(
        "return 1", "return 11").replace("return 2", "return 22").encode("utf-8")
    assert "2 hunk(s)" in result.output
    assert "hunk 1 @@ -3,2 +3,2 @@: applied at line 3" in result.output
    assert "hunk 2 @@ -6,2 +6,2 @@: applied at line 6" in result.output


def test_the_journal_sees_every_file_before_its_bytes_change(project, journal):
    """`record()` is the safety net, so it has to run while the old bytes are there."""
    original = (project / "src" / "two.py").read_bytes()
    assert not PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2")).error
    assert journal.calls[0]["bytes_at_record"] == original
    assert journal.calls[0]["tool"] == "patch"
    assert Path(journal.calls[0]["workdir"]).samefile(project)
    assert (project / "src" / "two.py").read_bytes() != original


def test_root_directs_the_paths_of_the_diff(project, journal):
    result = PatchTool().execute(diff=d(
        "--- a/two.py", "+++ b/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 7"),
        root="src")
    assert not result.error, result.output
    assert (project / "src" / "two.py").read_text(encoding="utf-8").startswith("alpha = 7")
    # `../x.py` under `root="src"` is not a bad root — `src` is a directory and the
    # guard let it through — so the refusal this call must produce is the one that
    # names the resolved path and says it is outside the root that was given. The
    # needle below used to ask for "not a directory", a claim the tool would only
    # make if `root` itself were broken, and it asserts the resolved path because
    # a refusal that does not name it is not a refusal this model can act on.
    refused(PatchTool().execute(diff=d("--- a/../x.py", "+++ b/../x.py", "@@ -0,0 +1,1 @@",
                                       "+x = 1"), root="src"),
            "outside the `root` you gave", "x.py")
    assert not (project / "x.py").exists(), "the refusal still created the file"


# ------------------------------------------------------------------ locating ---

def test_a_hunk_shifted_by_four_lines_is_applied_and_the_offset_said(project, journal):
    """The context is real and matches exactly one place: move it, and say so."""
    _write_lf(project / "shift.py",
              "a\nb\nc\nd\ne\nf\ng\nh\ndef target():\n    return 0\n")
    result = PatchTool().execute(diff=d(
        "--- a/shift.py", "+++ b/shift.py",
        "@@ -5,2 +5,2 @@", " def target():", "-    return 0", "+    return 42"))
    assert not result.error, result.output
    assert (project / "shift.py").read_bytes() == \
        b"a\nb\nc\nd\ne\nf\ng\nh\ndef target():\n    return 42\n"
    # `def target():` is the ninth line of the file written above (eight letters
    # first), so the hunk is applied AT 9 and the offset is 9 - 5 = 4 lines later,
    # exactly as the test's name says. The assertion below asked for "line 10",
    # which would be an offset of 5 and contradicts the same sentence's own
    # "4 line(s) later".
    assert "applied at line 9, 4 line(s) later than the declared line 5" in result.output
    assert result.output.count("4 line(s) later") == 1
    assert "1 hunk(s) applied at an OFFSET" in result.output
    assert result.metadata["offsets"] == [
        "shift.py: hunk 1 moved 4 line(s) later (declared 5, applied 9)"]


def test_a_hunk_shifted_earlier_is_reported_as_earlier(project, journal):
    _write_lf(project / "up.py", "x\ny\ndef f():\n    pass\n")
    result = PatchTool().execute(diff=d(
        "--- a/up.py", "+++ b/up.py", "@@ -3,1 +3,1 @@", "-y", "+y = 2"))
    assert not result.error, result.output
    assert (project / "up.py").read_bytes() == b"x\ny = 2\ndef f():\n    pass\n"
    assert "applied at line 2, 1 line(s) earlier than the declared line 3" in result.output


def test_a_stale_hunk_is_refused_and_names_the_line_that_disagrees(project, journal):
    """The context exists nowhere: refuse, quote both sides of the argument."""
    before = snapshot(project)
    result = refused(PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 99", "+    return 7")),
        "does not match src/one.py at line 4")
    assert "'    return 99'" in result.output, result.output
    assert "'    return 1'" in result.output, result.output
    assert "appears nowhere else" in result.output
    assert "No file was written" in result.output
    assert snapshot(project) == before


def test_a_hunk_past_the_end_of_the_file_says_so(project, journal):
    before = snapshot(project)
    # The hunk as this test first wrote it — `@@ -9,2 +9,2 @@` over `-beta = 2` and
    # `+beta = 3` — is one old and one new line, so its own counts lie and the
    # counts check refuses it before the file is ever read; and a well-formed
    # `-beta = 2` / `+beta = 3` pair would match `beta = 2` at line 2 uniquely and
    # be MOVED there rather than refused. To reach the "the file ends there" answer
    # the block has to be well-formed and still run past the last line: two old
    # lines declared at line 9 of a file that holds two.
    result = refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -9,2 +9,2 @@", " beta = 2", "-gone = 1", "+gone = 2")), "the file ends there")
    assert "does not match src/two.py at line 9" in result.output
    assert "beta = 2" in result.output, result.output
    assert snapshot(project) == before


def test_an_ambiguous_hunk_is_refused_and_its_candidate_lines_named(project, journal):
    """Two plausible places is not a coin toss: the model has to see the disagreement."""
    (project / "twins.py").write_text("def a():\n    return 1\n\ndef b():\n    return 1\n",
                                      encoding="utf-8")
    before = snapshot(project)
    result = refused(PatchTool().execute(diff=d(
        "--- a/twins.py", "+++ b/twins.py",
        "@@ -1,1 +1,1 @@", "-    return 1", "+    return 2")), "matches 2 places")
    assert "(lines 2, 5)" in result.output, result.output
    assert "Add context lines" in result.output
    assert snapshot(project) == before


def test_fuzzy_similarity_is_never_a_match(project, journal):
    """A context line one trailing space off is a refusal, not a near hit."""
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one(): ", "-    return 1", "+    return 2")),
        "does not match src/one.py at line 3")
    assert snapshot(project) == before


# --------------------------------------------------- all of it, or none of it ---

def test_a_half_applying_diff_leaves_every_file_byte_identical(project, journal):
    """The strongest claim this tool makes, asserted on bytes.

    Two of the three files would apply cleanly. The third carries a hunk whose
    context is stale, so the call has to write nothing anywhere - and say which
    file refused. A patch tool that applied the two good ones and mentioned the
    third in passing is what this test exists to make impossible.
    """
    before = snapshot(project)
    result = PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 1", "+    return 100",
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -1,2 +1,2 @@", "-alpha = 1", "-beta = 2", "+alpha = 9", "+beta = 9",
        "--- a/src/three.py", "+++ b/src/three.py",
        # `src/three.py` really does hold `def drop():` and `    pass`, so the hunk
        # as first written here matched it line for line and the diff applied
        # whole: nothing about it was stale. The removed name is the one that has
        # to disagree with the file for this test to be testing anything.
        "@@ -1,2 +1,1 @@", "-def dropped():", "-    pass", "+def never_mind():",
    ))
    assert result.error is True
    assert snapshot(project) == before, "a half-applied diff rewrote something"
    assert "src/three.py" in result.output
    assert "does not match src/three.py at line 1" in result.output
    assert "src/one.py, src/two.py" in result.output, \
        "the ready files have to be named as deliberately untouched"
    assert journal.paths == [], "a refusal must not even reach the journal"
    assert not list(project.rglob("*.beecode-tmp")), "no side file left behind"


def test_a_deleting_diff_that_cannot_apply_leaves_the_file_on_disk(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/three.py", "+++ /dev/null",
        "@@ -1,2 +0,0 @@", "-def something_else():", "-    pass")), "does not match")
    assert (project / "src" / "three.py").read_bytes() == before["src/three.py"]


def test_a_truncated_diff_writes_nothing_anywhere(project, journal):
    before = snapshot(project)
    result = refused(PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 1", "+    return 3",
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -1,2 +1,2 @@", "-alpha = 1", "-beta = 2", "+alpha = 0",
    )), "counts")
    assert "declares 2 old and 2 new lines" in result.output
    assert snapshot(project) == before


def test_counts_that_lie_are_refused_by_name(project, journal):
    """`@@ -1,9 +1,9 @@` over four lines is a broken diff, not a puzzle to solve."""
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py",
        "@@ -1,9 +1,9 @@", "-alpha = 1", "+alpha = 2", "-beta = 2", "+beta = 3")),
        "a hunk whose counts lie is refused")
    assert snapshot(project) == before


def test_one_bad_section_refuses_the_whole_patch(project, journal):
    before = snapshot(project)
    result = refused(PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return 1", "+    return 4",
        "index abc1234..def5678 160000",
    )), "submodule")
    assert "1 file(s), 1 hunk(s)" not in result.output
    assert snapshot(project) == before


# ----------------------------------------------------- create, delete, insert ---

def test_a_created_file_and_a_deleted_file_in_one_call(project, journal):
    # Bytes, not `write_text`: on Windows `write_text` turns the `\n` into `\r\n`
    # and the file the tool reports as gone is 6 bytes, not the 5 this test then
    # has to recount.
    _write_lf(project / "src" / "four.py", "four\n")
    result = PatchTool().execute(diff=d(
        "--- /dev/null", "+++ b/src/deep/new_mod.py",
        "@@ -0,0 +1,2 @@", "+# -*- coding: utf-8 -*-", "+READY = True",
        "--- a/src/four.py", "+++ /dev/null",
        "@@ -1,1 +0,0 @@", "-four",
    ))
    assert not result.error, result.output
    created = project / "src" / "deep" / "new_mod.py"
    assert created.read_bytes() == b"# -*- coding: utf-8 -*-\nREADY = True\n"
    assert not (project / "src" / "four.py").exists()
    assert f"src/deep/new_mod.py: created, 1 hunk(s), {created.stat().st_size} bytes written" \
        in result.output
    assert "src/four.py: removed, 1 hunk(s), the file's 5 bytes are gone from disk" \
        in result.output
    assert journal.actions == {"new_mod.py": "new", "four.py": "delete"}
    assert journal.calls[0]["bytes_at_record"] is None, "a create has no old bytes to lose"


def test_a_hunk_that_adds_to_an_empty_file(project, journal):
    (project / "zero.py").write_bytes(b"")
    result = PatchTool().execute(diff=d(
        "--- a/zero.py", "+++ b/zero.py",
        "@@ -0,0 +1,3 @@", "+first = 1", "+second = 2", "+third = 3"))
    assert not result.error, result.output
    assert (project / "zero.py").read_bytes() == b"first = 1\nsecond = 2\nthird = 3\n"
    assert journal.actions == {"zero.py": "modify"}     # the file was there, empty


def test_a_pure_insertion_at_the_declared_place(project, journal):
    result = PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,0 +2,1 @@", "+inserted = 1"))
    assert not result.error, result.output
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 1\ninserted = 1\nbeta = 2\n"


def test_an_insertion_outside_the_file_is_refused(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -7,0 +8,1 @@", "+far = 1")),
        "has only 2 line(s)")
    assert snapshot(project) == before


def test_an_insertion_claiming_an_empty_file_is_refused(project, journal):
    """`@@ -0,0` against a file with lines says the before-file had none. It had two."""
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -0,0 +1,1 @@", "+inserted = 1")),
        "has no lines before it")
    assert snapshot(project) == before


def test_a_deletion_that_leaves_lines_behind_is_refused(project, journal):
    (project / "half.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/half.py", "+++ /dev/null", "@@ -1,2 +0,0 @@", "-one", "-two")),
        "a deletion has to take the whole file")
    assert snapshot(project) == before


def test_a_creation_over_an_existing_file_is_refused(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- /dev/null", "+++ b/src/two.py", "@@ -0,0 +1,1 @@", "+overwrite = 1")),
        "already exists")
    assert snapshot(project) == before


def test_patching_a_file_that_is_not_there_is_refused(project, journal):
    refused(PatchTool().execute(diff=d(
        "--- a/src/nothing_here.py", "+++ b/src/nothing_here.py",
        "@@ -1,1 +1,1 @@", "-x = 1", "+x = 2")), "does not declare it as created")


def test_a_section_with_no_hunk_is_refused_rather_than_counted(project, journal):
    """`--- a/x` + `+++ b/x` with no hunk changes nothing; saying so is not a lie-free pass."""
    refused(PatchTool().execute(diff=d("--- a/src/two.py", "+++ b/src/two.py")),
            "changes nothing")
    refused(PatchTool().execute(diff=d("--- a/src/two.py", "+++ /dev/null")),
            "deleting a file without the hunks")


# ------------------------------------------------- line endings and encodings ---

def test_a_crlf_file_patched_with_an_lf_diff_keeps_its_own_endings(project, journal):
    (project / "win.py").write_bytes(b"a = 1\r\nb = 2\r\n\r\nc = 3\r\n")
    result = PatchTool().execute(diff=d(
        "--- a/win.py", "+++ b/win.py",
        "@@ -1,2 +1,2 @@", " a = 1", "-b = 2", "+b = 20",
        "@@ -4,1 +4,2 @@", "-c = 3", "+c = 30", "+d = 40"))
    assert not result.error, result.output
    assert (project / "win.py").read_bytes() == b"a = 1\r\nb = 20\r\n\r\nc = 30\r\nd = 40\r\n"


def test_a_crlf_diff_still_lands_in_a_crlf_file_without_doubling(project, journal):
    """The patch itself came from Windows: its own CRs are not content."""
    (project / "win2.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    result = PatchTool().execute(diff=crlf(
        "--- a/win2.py", "+++ b/win2.py", "@@ -1,2 +1,2 @@", " a = 1", "-b = 2", "+b = 3"))
    assert not result.error, result.output
    data = (project / "win2.py").read_bytes()
    assert data == b"a = 1\r\nb = 3\r\n"
    assert b"\r\r" not in data, "the diff's own line endings leaked into the file"


def test_an_lf_file_stays_lf_under_a_crlf_diff(project, journal):
    (project / "unix.py").write_bytes(b"a = 1\nb = 2\n")
    result = PatchTool().execute(diff=crlf(
        "--- a/unix.py", "+++ b/unix.py", "@@ -1,1 +1,1 @@", "-a = 1", "+a = 9"))
    assert not result.error, result.output
    assert (project / "unix.py").read_bytes() == b"a = 9\nb = 2\n"


def test_a_cyrillic_file_reports_bytes_and_not_characters(project, journal):
    """The count the model repeats to the user has to be the file's, in bytes."""
    (project / "платежи.py").write_bytes(
        "def обработка():\n    return 'оплата'\n".encode("utf-8"))
    result = PatchTool().execute(diff=d(
        "--- a/платежи.py", "+++ b/платежи.py",
        "@@ -1,2 +1,2 @@", " def обработка():", "-    return 'оплата'", "+    return 'взнос'"))
    assert not result.error, result.output.encode("utf-8").hex()
    text = (project / "платежи.py").read_text(encoding="utf-8")
    assert text == "def обработка():\n    return 'взнос'\n"
    written = (project / "платежи.py").stat().st_size
    assert written == len(text.encode("utf-8"))
    assert written > len(text), "the point of the test: bytes are not characters"
    assert f"{written} bytes written" in result.output
    # Proof with non-ASCII in it goes to a UTF-8 file, never to a cp1251 console.
    (project / "proof-bytes.txt").write_bytes(
        f"{result.output}\n---\n{text}\n".encode("utf-8"))
    assert f"{written} bytes written" in (project / "proof-bytes.txt").read_text(
        encoding="utf-8")


def test_a_created_cyrillic_file_is_valid_utf8(project, journal):
    result = PatchTool().execute(diff=d(
        "--- /dev/null", "+++ b/отчёт.md", "@@ -0,0 +1,1 @@", "+# Отчёт за приёмку"))
    assert not result.error, result.output
    raw = (project / "отчёт.md").read_bytes()
    assert raw == "# Отчёт за приёмку\n".encode("utf-8")
    assert result.metadata["bytes"] == len(raw)


def test_a_utf8_bom_survives_a_patch_and_does_not_break_the_first_line(project, journal):
    (project / "bom.py").write_bytes("﻿a = 1\nb = 2\n".encode("utf-8"))
    result = PatchTool().execute(diff=d(
        "--- a/bom.py", "+++ b/bom.py", "@@ -1,2 +1,2 @@", " a = 1", "-b = 2", "+b = 3"))
    assert not result.error, result.output
    assert (project / "bom.py").read_bytes() == "﻿a = 1\nb = 3\n".encode("utf-8")


def test_a_file_that_is_genuinely_not_utf8_is_refused_and_its_bytes_survive(project, journal):
    payload = "# платёж — приёмка\nx = 1\n"
    victim = payload.encode("cp1251")
    with pytest.raises(UnicodeDecodeError):
        victim.decode("utf-8")                # the refusal below has a real reason
    (project / "cp1251.py").write_bytes(victim)
    refused(PatchTool().execute(diff=d(
        "--- a/cp1251.py", "+++ b/cp1251.py", "@@ -2,1 +2,1 @@", "-x = 1", "+x = 2")),
        "not UTF-8 text")
    assert (project / "cp1251.py").read_bytes() == victim


def test_no_newline_at_end_of_file_is_honoured(project, journal):
    (project / "tail.py").write_bytes(b"a\nb\nc")
    result = PatchTool().execute(diff=d(
        "--- a/tail.py", "+++ b/tail.py",
        "@@ -2,2 +2,2 @@", " b", "-c", "+c = 3", "\\ No newline at end of file"))
    assert not result.error, result.output
    assert (project / "tail.py").read_bytes() == b"a\nb\nc = 3"


def test_an_added_line_can_restore_the_missing_newline(project, journal):
    (project / "tail2.py").write_bytes(b"a\nb")
    result = PatchTool().execute(diff=d(
        "--- a/tail2.py", "+++ b/tail2.py",
        "@@ -2,1 +2,1 @@", "-b", "+b\n"))
    assert not result.error, result.output
    assert (project / "tail2.py").read_bytes() == b"a\nb\n"


def test_a_no_newline_claim_that_does_not_match_the_file_is_refused(project, journal):
    """`\\ No newline at end of file` that the bytes contradict is a disagreement, not a hint."""
    (project / "terminated.py").write_bytes(b"a\nb\n")
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/terminated.py", "+++ b/terminated.py",
        "@@ -1,2 +1,1 @@", " a", "-b", "\\ No newline at end of file")),
        "does end with a newline")
    assert snapshot(project) == before


# ------------------------------------------------------------------- escapes ---

def test_a_traversal_path_is_refused_and_nothing_is_written_anywhere(project, journal, outside):
    target = outside / "authorized_keys"
    rel = os.path.relpath(str(target), os.getcwd()).replace(os.sep, "/")
    assert rel.startswith(".."), f"not an escape after all: {rel}"
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        f"--- a/{rel}", f"+++ b/{rel}", "@@ -1,1 +1,1 @@",
        "-ssh-rsa AAAA-the-users-own-key", "+ssh-rsa AAAA-attacker-key")),
        "OUTSIDE the working directory")
    assert target.read_bytes() == VICTIM
    assert snapshot(project) == before
    assert journal.paths == []
    assert not list(outside.glob("*.beecode-tmp"))


def test_an_absolute_path_into_another_folder_is_refused(project, journal, outside):
    victim = outside / "authorized_keys"
    refused(PatchTool().execute(diff=d(
        f"--- {victim}", f"+++ {victim}", "@@ -1,1 +1,1 @@",
        "-ssh-rsa AAAA-the-users-own-key", "+stolen")), "OUTSIDE the working directory")
    assert victim.read_bytes() == VICTIM


def test_a_tilde_path_is_refused_even_though_home_sits_inside_the_tree(project, journal,
                                                                      monkeypatch):
    """conftest points HOME inside tmp_path, so realpath alone would happily allow it."""
    monkeypatch.setenv("HOME", str(project / "fake-home"))
    monkeypatch.setenv("USERPROFILE", str(project / "fake-home"))
    # conftest already redirects HOME into `tmp_path/fake-home` and creates it, so
    # this is the same folder, not a new one: `mkdir()` alone loses the race with
    # the fixture that made the redirect true.
    (project / "fake-home").mkdir(parents=True, exist_ok=True)
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/~/.ssh/authorized_keys", "+++ b/~/.ssh/authorized_keys",
        "@@ -0,0 +1,1 @@", "+ssh-rsa AAAA-attacker-key")), "home directory")
    assert snapshot(project) == before
    assert not (project / "fake-home" / ".ssh").exists()


def test_a_root_parameter_confines_every_path_of_the_diff(project, journal):
    (project / "sub").mkdir()
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/../src/two.py", "+++ b/../src/two.py",
        "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2"), root="sub"),
        "outside the `root`")
    assert snapshot(project) == before


@pytest.mark.skipif(os.name != "nt", reason="alternate data streams are an NTFS feature")
def test_an_alternate_data_stream_name_is_refused_and_no_stream_appears(project, journal):
    victim = project / "src" / "one.py"
    refused(PatchTool().execute(diff=d(
        "--- a/src/one.py:evil.txt", "+++ b/src/one.py:evil.txt",
        "@@ -0,0 +1,1 @@", "+<svg/>")), "not an ordinary file name")
    with pytest.raises(OSError):
        open(str(victim) + ":evil.txt", "rb").read()
    assert victim.read_text(encoding="utf-8") == GOOD_FILE


def test_a_link_out_of_the_tree_is_refused_by_the_path_it_really_resolves_to(
        project, journal, outside):
    if not make_escape_link(project, outside, "elsewhere"):
        pytest.skip("this box allows no symlinks")
    refused(PatchTool().execute(diff=d(
        "--- a/elsewhere/authorized_keys", "+++ b/elsewhere/authorized_keys",
        "@@ -1,1 +1,1 @@", "-ssh-rsa AAAA-the-users-own-key", "+attacked")),
        "OUTSIDE the working directory")
    assert (outside / "authorized_keys").read_bytes() == VICTIM
    assert (project / "elsewhere").is_symlink(), "the refusal replaced the link"


def test_a_directory_as_a_file_is_refused(project, journal):
    refused(PatchTool().execute(diff=d(
        "--- a/src", "+++ b/src", "@@ -1,1 +1,1 @@", "-x", "+y")), "not a file")


# --------------------------------------------- what a diff may not declare ---

def test_a_mode_change_is_refused_instead_of_pretended(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "diff --git a/src/two.py b/src/two.py", "old mode 100644", "new mode 100755",
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2")),
        "mode change")
    assert snapshot(project) == before


def test_a_binary_patch_is_refused(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "diff --git a/logo.png b/logo.png", "index 3b3f2f1..7c1d0a2 100644", "GIT binary patch",
        "literal 42", "zcmckS!9zYT@2K0Kb0OtL110")), "binary patch")
    assert snapshot(project) == before


def test_binary_files_line_is_refused(project, journal):
    refused(PatchTool().execute(diff=d(
        "Binary files a/logo.png and b/logo.png differ")), "binary patch")


def test_a_rename_is_refused(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "diff --git a/src/two.py b/src/renamed.py", "similarity index 100%",
        "rename from src/two.py", "rename to src/renamed.py")), "rename")
    assert snapshot(project) == before


def test_a_header_naming_two_files_is_refused(project, journal):
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/other.py", "@@ -1,1 +1,1 @@",
        "-alpha = 1", "+alpha = 2")), "two different files")
    assert snapshot(project) == before
    assert not (project / "src" / "other.py").exists()


def test_an_executable_new_file_is_refused(project, journal):
    refused(PatchTool().execute(diff=d(
        "diff --git a/tool.sh b/tool.sh", "new file mode 100755",
        "--- /dev/null", "+++ b/tool.sh", "@@ -0,0 +1,1 @@", "+#!/bin/sh")), "executable")
    assert not (project / "tool.sh").exists()


def test_a_submodule_entry_is_refused(project, journal):
    refused(PatchTool().execute(diff=d(
        "diff --git a/vendor b/vendor", "index abc1234..def5678 160000",
        "--- a/vendor", "+++ b/vendor", "@@ -1,1 +1,1 @@",
        "-Subproject commit abc1234", "+Subproject commit def5678")), "submodule")


# ------------------------------------------------------------- shapes accepted ---

def test_a_git_style_diff_with_index_lines_applies(project, journal):
    result = PatchTool().execute(diff=d(
        "diff --git a/src/two.py b/src/two.py", "index 5c2f7a9..b1d0e44 100644",
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -2,1 +2,1 @@", "-beta = 2", "+beta = 22",
        "diff --git a/src/new2.py b/src/new2.py", "new file mode 100644",
        "index 0000000..e69de29", "--- /dev/null", "+++ b/src/new2.py",
        "@@ -0,0 +1,1 @@", "+NEW = 1"))
    assert not result.error, result.output
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 1\nbeta = 22\n"
    assert (project / "src" / "new2.py").read_bytes() == b"NEW = 1\n"


def test_paths_without_the_a_and_b_prefix_are_taken_literally(project, journal):
    result = PatchTool().execute(diff=d(
        "--- src/two.py", "+++ src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 3"))
    assert not result.error, result.output
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 3\nbeta = 2\n"


def test_a_pasted_fence_is_read(project, journal):
    result = PatchTool().execute(diff="```diff\n" + d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 5")
        + "```\n")
    assert not result.error, result.output
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 5\nbeta = 2\n"


def test_a_removed_line_that_looks_like_a_header_is_still_content(project, journal):
    """`-- x` removed prints as `--- x`: the counts, not the shape, end the hunk."""
    _write_lf(project / "rules.py", "keep\n-- drop me\nkeep too\n")
    result = PatchTool().execute(diff=d(
        "--- a/rules.py", "+++ b/rules.py", "@@ -1,3 +1,3 @@",
        # One `-` is the hunk's own marker, so the line as it reaches the patch is
        # `--- drop me`: the file's `-- drop me` with the marker in front of it.
        # The four dashes this test first wrote left a third dash inside the
        # content, and the tool was right to answer that the file has no
        # `--- drop me` in it. The hunk still ends on its counts, which are the
        # only thing that can tell `--- drop me` (content) from `--- a/rules.py`
        # (a header).
        " keep", "--- drop me", "+-- use this instead", " keep too"))
    assert not result.error, result.output
    assert (project / "rules.py").read_bytes() == b"keep\n-- use this instead\nkeep too\n"


def test_blank_context_lines_survive(project, journal):
    """Two spellings of an empty context line, and neither ends the hunk.

    A context line carries one leading space and then its own text, so the blank
    line in the middle of the window is `"  "`'s little sibling: `git` writes it as
    a single space, and a patch whose trailing whitespace was trimmed on the way
    here writes it as nothing. The body of this hunk first arrived as
    `"import os"` — no marker at all — and the tool was right to refuse it: a line
    that starts with arbitrary text is not a hunk line, and reading it as one is
    how a `--- a/path` header would end up as content.
    """
    _write_lf(project / "src" / "one_spaced.py", GOOD_FILE)
    for target, blank in (("src/one.py", " "), ("src/one_spaced.py", "")):
        result = PatchTool().execute(diff=d(
            f"--- a/{target}", f"+++ b/{target}",
            "@@ -1,4 +1,4 @@", " import os", blank, " def one():",
            "-    return 1", "+    return 8"))
        assert not result.error, result.output
        assert (project / target).read_bytes() == GOOD_FILE.replace(
            "    return 1", "    return 8").encode("utf-8")


def test_text_that_is_not_a_unified_diff_is_refused(project, journal):
    before = snapshot(project)
    for body in ("please change two.py for me", "*** 1,4 ****\n  alpha = 1\n",
                 "@@ -1 +1 @@\n-x\n+y", "--- a/src/two.py"):
        result = refused(PatchTool().execute(diff=body), "patch")
        assert "REFUSED" in result.output
    assert snapshot(project) == before


# ------------------------------------------------------------- the journal gate ---

def test_without_a_journal_nothing_is_written(project, monkeypatch):
    """The tool's own rule: no undo, no patch, and it says the bytes are unrecoverable."""
    import beeagent.core as core_package

    monkeypatch.delitem(sys.modules, "beeagent.core.journal", raising=False)
    monkeypatch.setattr(core_package, "journal", None, raising=False)
    before = snapshot(project)
    result = refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2")),
        "no undo journal", "could not be given back")
    assert "Nothing was written" in result.output
    assert result.metadata["refused"] == "no-journal"
    assert snapshot(project) == before


def test_a_journal_that_raises_stops_every_write(project, journal):
    before = snapshot(project)
    journal.boom = RuntimeError("disk full")
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2",
        "--- a/src/three.py", "+++ b/src/three.py", "@@ -1,2 +0,0 @@",
        "-def drop():", "-    pass")), "disk full")
    assert snapshot(project) == before


def test_a_journal_that_reports_failure_is_believed(project, journal):
    before = snapshot(project)
    journal.returns = False
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2")),
        "stored nothing")
    assert snapshot(project) == before


def test_the_import_is_guarded_for_a_machine_without_the_module(project, monkeypatch):
    """The guarded import runs for real: no stub, no crash - a refusal instead."""
    monkeypatch.setattr(patch_mod, "journal_record", lambda: None)
    assert patch_mod.journal_record() is None
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 2")),
        "journal")
    assert snapshot(project) == before


def test_the_guarded_import_finds_the_module_when_it_is_there(project, journal):
    assert callable(patch_mod.journal_record())
    assert patch_mod.journal_record() == journal.record


# ------------------------------------------------------------------ staleness ---

def test_a_file_the_user_saved_since_it_was_read_is_not_patched(project, journal):
    """The same rule `edit` applies: the diff was written against bytes that are gone."""
    target = project / "src" / "two.py"
    _seen.remember(target)                      # what `read` would have stamped
    target.write_text("alpha = 1\nbeta = changed by the user\n", encoding="utf-8")
    before = snapshot(project)
    refused(PatchTool().execute(diff=d(
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -2,1 +2,1 @@", "-beta = 2", "+beta = 3")),
        "changed since you read it")
    assert snapshot(project) == before


def test_our_own_patch_does_not_block_the_next_one(project, journal):
    tool = PatchTool()
    first = tool.execute(diff=d("--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@",
                                "-alpha = 1", "+alpha = 2"))
    assert not first.error, first.output
    second = tool.execute(diff=d("--- a/src/two.py", "+++ b/src/two.py", "@@ -2,1 +2,1 @@",
                                 "-beta = 2", "+beta = 3"))
    assert not second.error, second.output
    assert (project / "src" / "two.py").read_bytes() == b"alpha = 2\nbeta = 3\n"


def test_a_file_that_fails_to_write_is_reported_as_a_partial_apply(project, journal,
                                                                  monkeypatch):
    """A disk error halfway through is the one case that can leave a mixed tree."""
    real = patch_mod.write_text_preserving
    calls = []

    def flaky(path, text):
        calls.append(str(path))
        if len(calls) == 2:
            raise OSError("device not configured")
        return real(path, text)

    monkeypatch.setattr(patch_mod, "write_text_preserving", flaky)
    untouched = (project / "src" / "two.py").read_bytes()
    result = PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py", "@@ -3,2 +3,2 @@", " def one():",
        "-    return 1", "+    return 7",
        "--- a/src/two.py", "+++ b/src/two.py", "@@ -1,1 +1,1 @@", "-alpha = 1", "+alpha = 9"))
    assert result.error is True
    assert "PARTIALLY APPLIED" in result.output
    assert "undo journal" in result.output
    assert (project / "src" / "one.py").read_bytes() != GOOD_FILE.encode("utf-8")
    assert (project / "src" / "two.py").read_bytes() == untouched, "the failed file must be intact"
    assert result.metadata["partial"] == ["src/two.py"]
    assert not list(project.rglob("*.beecode-tmp")), "the side file has to be cleaned up"


# ------------------------------------------------------------------- language ---

def test_the_refusal_comes_back_in_russian_and_as_valid_utf8(project, journal, monkeypatch):
    monkeypatch.setattr(i18n, "_lang", "ru")
    result = PatchTool().execute(diff=d(
        "--- a/src/one.py", "+++ b/src/one.py",
        "@@ -3,2 +3,2 @@", " def one():", "-    return nonexistent", "+    return 5"))
    assert result.error is True
    proof = project / "proof-ru.txt"
    proof.write_bytes(result.output.encode("utf-8"))
    written = proof.read_bytes()
    for phrase in ("ОТКАЗАЛ", "Ни один файл не записан", "не совпадает с src/one.py в строке 4",
                   "перестрой хатку"):
        assert phrase.encode("utf-8") in written, phrase.encode("utf-8").hex()
    assert (project / "src" / "one.py").read_bytes() == GOOD_FILE.encode("utf-8")
    assert written.decode("utf-8") == result.output


def test_an_applied_patch_names_its_offsets_in_the_active_language(project, journal,
                                                                  monkeypatch):
    # `write_bytes`, so the file is LF on disk and the byte-for-byte assertion
    # below is the same on every platform.
    (project / "shift2.py").write_bytes(b"a\nb\nc\nd\ne\nf\ng\nh\nX = 1\n")
    monkeypatch.setattr(i18n, "_lang", "ru")
    result = PatchTool().execute(diff=d(
        "--- a/shift2.py", "+++ b/shift2.py", "@@ -4,1 +4,1 @@", "-X = 1", "+X = 2"))
    assert not result.error, result.output.encode("utf-8").hex()
    proof = project / "proof-ru-applied.txt"
    proof.write_bytes(result.output.encode("utf-8"))
    written = proof.read_bytes()
    # `X = 1` is the ninth line and was declared at the fourth, so the shift is
    # 9 - 4 = 5 lines later, not the 4 this test first asked for. And the 4 it
    # asked for was looked for in the wrong sentence besides: the report prints the
    # per-hunk note, while the `moved N line(s)` line is the machine-readable
    # `metadata["offsets"]` entry. Both are checked here, in Russian.
    for phrase in ("патч применён", "СО СМЕЩЕНИЕМ",
                   "применена на строке 9, на 5 строк позже заявленной 4"):
        assert phrase.encode("utf-8") in written, phrase.encode("utf-8").hex()
    assert result.metadata["offsets"] == [
        "shift2.py: хатка 1 сдвинулась на 5 строк позже (заявлено 4, применено 9)"]
    assert (project / "shift2.py").read_bytes() == b"a\nb\nc\nd\ne\nf\ng\nh\nX = 2\n"
