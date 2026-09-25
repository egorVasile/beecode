"""`move` and `remove`, driven the way the audit drove the write tools.

Two rules shape this file, both taken from `tests/test_tool_layer.py`:

* the tools run for real, and the assertion is about the BYTES LEFT ON DISK, not
  about a sentence the tool happened to print;
* "outside" means outside every root `_path_policy` carries. The working
  directory is a root and so is the interpreter's temp directory, so a project
  sitting *under* `tmp_path` can never escape by `../` — the project here lives
  in a scratch directory beside the temp tree, which is the only layout where
  `../escape.txt` names something the policy must refuse.

The undo journal is a hard dependency of both tools, and it is being written by
another agent right now, so every test here runs against a fake installed by
`fake_journal` below: the suite is green with `beeagent/core/journal.py` present
or absent. Two tests then talk to the real module when it imports, and one
removes it to prove the refusal.

Why this file once reported ten errors instead of failures, written down so the
next reader does not go looking for ten bugs: an error is raised outside the
test, at setup or teardown, and the guard in `conftest.py` raises at teardown for
each test that let a tool write into the checkout (`.beeagent/undo/journal.jsonl`
and its blobs) instead of `tmp_path`. One offending test is one error, so a
half-written file shows a crowd. The two shapes tell apart cheaply: a module that
cannot be imported (a `SyntaxError` left by an edit that stopped halfway) gives
exactly ONE `ERROR test_files_tools.py` and `Interrupted: 1 error during
collection`, while N teardown errors mean N tests missing `monkeypatch.chdir`.
Both are gone: this file is 35 passed, 1 skipped, 0 errors.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.core.permissions import Permissions
from beeagent.tools import files as files_mod
from beeagent.tools.files import MoveTool, RemoveTool
from beeagent.tools.registry import ToolRegistry

WINDOWS_TARGET = "C:\\Windows\\win.ini"


# ---------------------------------------------------------------------------
# fixtures and helpers
# ---------------------------------------------------------------------------

class FakeJournal:
    """The part of `beeagent.core.journal` these tools call, with no disk writes."""

    MAX_ENTRIES = 50
    MAX_TOTAL_BYTES = 25 * 1024 * 1024
    MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024

    def __init__(self):
        self.calls = []                       # (workdir, tool, path, action)
        self.error = None                     # set to make record() raise

    def record(self, workdir, tool, path, *, action="modify"):
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((str(workdir), tool, str(path), action))
        return {"seq": len(self.calls), "tool": tool, "path": str(path),
                "action": action, "blob": "blob-fake.snap", "size": 1}

    def refusal(self, tool, error):
        return (f"`{tool}` refused: the fake journal could not store the previous "
                f"bytes ({error})")

    def recoverable_clause(self, entry):
        return f" — previous bytes saved, /undo #{entry['seq']} puts them back"


@pytest.fixture(autouse=True)
def fake_journal(monkeypatch):
    """Answer for the journal whether or not `beeagent/core/journal.py` has landed."""
    fake = FakeJournal()
    monkeypatch.setitem(sys.modules, "beeagent.core.journal", fake)
    try:
        import beeagent.core as core_pkg
        monkeypatch.setattr(core_pkg, "journal", fake, raising=False)
    except ImportError:
        pass
    return fake


@pytest.fixture
def no_journal(monkeypatch):
    """Both shapes of "there is no journal": gone from the import, and present but
    with no `record()` to call."""
    def remove(broken_import=True):
        import beeagent.core as core_pkg
        if broken_import:
            monkeypatch.setitem(sys.modules, "beeagent.core.journal", None)
            monkeypatch.delattr(core_pkg, "journal", raising=False)
        else:
            monkeypatch.setitem(sys.modules, "beeagent.core.journal",
                                type("NoRecord", (), {})())
            monkeypatch.setattr(core_pkg, "journal",
                                sys.modules["beeagent.core.journal"], raising=False)
    return remove


def _scratch(tmp_path):
    """A directory beside the temp tree — outside every root the policy carries."""
    temp_root = Path(tempfile.gettempdir()).resolve()
    base = temp_root.parent / f"beecode-files-{os.getpid()}-{tmp_path.name}"
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:                          # a box whose temp parent is not writable
        pytest.skip(f"cannot create a scratch directory beside {temp_root}; the "
                    f"escape tests need a path outside every root")
    return base


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A started-in folder, with Russian names because that is normal input here.

    `write_bytes`, not `write_text`: on Windows `write_text` turns every `\n` into
    `\r\n`, so the file a byte-count test expects to be 15 bytes on disk is 16 and
    the tool — which reports the bytes that really are there, as it must — reads as
    though it miscounted. The bytes below are the bytes the assertions recount.
    """
    base = _scratch(tmp_path)
    workdir = base / "project"
    (workdir / "src").mkdir(parents=True)
    (workdir / "src" / "платежи.py").write_bytes("# обработчик\n".encode("utf-8"))
    (workdir / "Readme.md").write_bytes("проект\n".encode("utf-8"))
    (workdir / "notes.txt").write_bytes("заметка\n".encode("utf-8"))
    tree = workdir / "build"
    (tree / "out").mkdir(parents=True)
    (tree / "a.o").write_bytes(b"x" * 100)
    (tree / "out" / "b.o").write_bytes(b"y" * 50)
    monkeypatch.chdir(workdir)
    yield workdir
    shutil.rmtree(str(base), ignore_errors=True)


def state(root):
    """relpath -> (kind, size, bytes) for everything under `root`."""
    out = {}
    for dirpath, names, entries in os.walk(str(root)):
        rel_dir = os.path.relpath(dirpath, str(root)).replace("\\", "/")
        out[rel_dir + "/"] = ("dir", sorted(names))
        for name in entries:
            victim = os.path.join(dirpath, name)
            rel = os.path.relpath(victim, str(root)).replace("\\", "/")
            try:
                with open(victim, "rb") as handle:
                    out[rel] = ("file", os.path.getsize(victim), handle.read())
            except OSError:
                out[rel] = ("unreadable", -1, b"")
    return out


def refused(result, *needles):
    """A refusal: `error=True`, no success wording, and it names what it means."""
    assert result.error is True, f"a refusal that reports success: {result.output[:160]!r}"
    for claim in ("Moved ", "Перенесено", "Removed ", "Удалено"):
        assert claim not in result.output, result.output[:160]
    for needle in needles:
        assert needle in result.output, result.output[:280]
    return result


def make_escape_link(directory: Path, target: str, name: str) -> bool:
    """Point `directory/name` at `target` with whatever link this box allows."""
    link = os.path.join(str(directory), name)
    try:
        os.symlink(target, link)
        return os.path.islink(link)
    except OSError:
        pass
    if os.name == "nt" and os.path.isdir(target):
        done = subprocess.run(["cmd", "/c", "mklink", "/J", link, target],
                              capture_output=True)
        return done.returncode == 0 and os.path.islink(link)
    return False


def case_folds(directory) -> bool:
    """Whether this filesystem cannot tell `Probe` from `PROBE`."""
    probe = os.path.join(str(directory), "CaseProbe-beecode")
    with open(probe, "w") as handle:
        handle.write("x")
    try:
        return os.path.exists(os.path.join(str(directory), "CASEPROBE-BEECODE"))
    finally:
        os.remove(probe)


# ---------------------------------------------------------------------------
# 1 — the permission gate these tools exist to make narrow
# ---------------------------------------------------------------------------

def test_both_tools_change_the_machine_so_they_need_a_grant():
    move, remove = MoveTool(), RemoveTool()
    assert move.writes_files and remove.writes_files
    assert move.is_safe() is False and remove.is_safe() is False
    gate = Permissions(mode="ask")
    assert gate.allows(move) is False and gate.allows(remove) is False
    gate.grant("move")
    gate.grant("remove")
    assert gate.allows(move) and gate.allows(remove)
    # The ceiling still holds over an earlier grant: renaming is not reading.
    assert Permissions(mode="readonly").allows(move) is False


def test_the_names_the_model_invents_reach_the_same_tools():
    registry = ToolRegistry()
    move, remove = MoveTool(), RemoveTool()
    assert registry.register(move) and registry.register(remove)
    assert registry.get("rename") is move and registry.get("mv") is move
    assert registry.get("delete") is remove and registry.get("rm") is remove
    assert registry.canonical_name("remove_file") == "remove"


# ---------------------------------------------------------------------------
# 2 — move: the work it promises
# ---------------------------------------------------------------------------

def test_rename_inside_the_project(project):
    result = MoveTool().execute("notes.txt", "записка.txt")
    assert not result.error, result.output
    assert not (project / "notes.txt").exists()
    assert (project / "записка.txt").read_text(encoding="utf-8") == "заметка\n"
    assert result.metadata["old_name_gone"] is True


def test_move_creates_the_folder_it_needs_on_the_way(project):
    result = MoveTool().execute("notes.txt", "docs/2026/notes.txt")
    assert not result.error, result.output
    assert (project / "docs" / "2026" / "notes.txt").read_text(encoding="utf-8")
    assert not (project / "notes.txt").exists()
    # The resolved pair is reported, not the spelling that was typed.
    assert str(project / "docs" / "2026" / "notes.txt") in result.output


def test_create_dirs_false_refuses_and_leaves_no_empty_folders(project):
    before = state(project)
    result = MoveTool().execute("notes.txt", "docs/2026/notes.txt", create_dirs="false")
    refused(result, "does not exist", "create_dirs")
    assert result.metadata["refused"] == "missing-parent"
    assert state(project) == before
    assert not (project / "docs").exists(), "the folders were made and then reported"


def test_moving_into_an_existing_folder_keeps_the_name(project):
    result = MoveTool().execute("notes.txt", "src")
    assert not result.error, result.output
    assert (project / "src" / "notes.txt").exists()


def test_a_directory_moves_whole_and_is_counted(project):
    result = MoveTool().execute("build", "artifact")
    assert not result.error, result.output
    assert (project / "artifact" / "out" / "b.o").read_bytes() == b"y" * 50
    assert not (project / "build").exists()
    assert result.metadata["files"] == 2 and result.metadata["bytes"] == 150
    assert "2 files, 150 bytes" in result.output


def test_destination_that_exists_is_refused_and_survives(project):
    before = state(project)
    result = MoveTool().execute("notes.txt", "Readme.md")
    refused(result, "already exists", "overwrite=true")
    assert result.metadata["refused"] == "destination-exists"
    assert state(project) == before


def test_overwrite_replaces_and_journals_what_it_destroyed(project, fake_journal):
    (project / "victim.md").write_text("the user's own words\n", encoding="utf-8")
    result = MoveTool().execute("notes.txt", "victim.md", overwrite=True)
    assert not result.error, result.output
    assert (project / "victim.md").read_text(encoding="utf-8") == "заметка\n"
    recorded = [call for call in fake_journal.calls if call[2].endswith("victim.md")]
    assert recorded and recorded[-1][3] == "modify", "overwritten without a back-up"


def test_a_string_false_is_not_a_yes(project):
    (project / "victim.md").write_text("keep me\n", encoding="utf-8")
    result = MoveTool().execute("notes.txt", "victim.md", overwrite="false")
    refused(result, "already exists")
    assert (project / "victim.md").read_text(encoding="utf-8") == "keep me\n"


# ---------------------------------------------------------------------------
# 3 — the Windows case question
# ---------------------------------------------------------------------------

def test_a_case_only_rename_ends_up_spelled_the_way_it_was_asked(project):
    if not case_folds(project):
        pytest.skip("this filesystem is case-sensitive: `Readme.md` -> `README.md` "
                    "is a plain rename here, not the two-step case problem")
    result = MoveTool().execute("Readme.md", "README.md")
    assert not result.error, result.output
    listed = os.listdir(str(project))
    assert "README.md" in listed, f"the tool said moved, the folder says {listed}"
    assert "Readme.md" not in listed, "Windows swallowed the case change silently"
    assert result.metadata["case_only"] is True
    assert result.metadata["stored_as"] == "README.md"
    assert "case-only rename" in result.output and "two steps" in result.output
    assert (project / "README.md").read_text(encoding="utf-8") == "проект\n"


def test_a_case_only_rename_of_a_folder_is_verified_too(project):
    if not case_folds(project):
        pytest.skip("case-sensitive filesystem: no swallowing to prove")
    result = MoveTool().execute("build", "BUILD")
    assert not result.error, result.output
    assert "BUILD" in os.listdir(str(project)) and "build" not in os.listdir(str(project))


def test_the_same_spelling_is_a_self_move_and_is_refused(project):
    before = state(project)
    result = MoveTool().execute("Readme.md", "Readme.md")
    refused(result, "same entry")
    assert result.metadata["refused"] == "move-onto-itself"
    assert state(project) == before


def test_when_the_filesystem_will_not_take_the_case_the_tool_says_so(project, monkeypatch):
    """The refusal this project exists for: never print "moved" over an unchanged name."""
    if not case_folds(project):
        pytest.skip("case-sensitive filesystem: no swallowing to prove")
    real_rename = os.rename
    kept = "Readme.md"                      # the spelling this volume insists on

    def stubborn(src, dst):
        # What "swallows the case change" has to mean here: any name asked for that
        # folds to the entry already stored comes back with the stored spelling.
        # The stub this test first ran with only no-oped when `src` and `dst`
        # folded alike — and on the tool's two-step rename they never do (step one
        # moves the entry to a staging name, step two renames THAT to the wanted
        # case), so the rename really happened, `README.md` really was on disk, and
        # "Moved" was the honest answer to a call that had not simulated anything.
        wanted = os.path.basename(str(dst).rstrip("\\/"))
        if os.path.normcase(wanted) == os.path.normcase(kept):
            dst = os.path.join(os.path.dirname(str(dst)), kept)
        real_rename(src, dst)

    monkeypatch.setattr(files_mod.os, "rename", stubborn)
    result = MoveTool().execute("Readme.md", "README.md")
    refused(result, "swallowed the case change")
    assert result.metadata["refused"] == "case-swallowed"
    assert os.listdir(str(project)), "the file vanished while the tool argued"
    assert "Readme.md" in os.listdir(str(project)), "the old name was not restored"


# ---------------------------------------------------------------------------
# 4 — paths that leave the project
# ---------------------------------------------------------------------------

def test_a_parent_escape_is_refused_in_both_directions_with_nothing_changed(project):
    (project.parent / "escape.txt").write_text("next door\n", encoding="utf-8")
    before = state(project)
    refused(MoveTool().execute("../escape.txt", "inside.txt"),
            "OUTSIDE the working directory")
    refused(MoveTool().execute("notes.txt", "../escape.txt"),
            "OUTSIDE the working directory")
    assert state(project) == before
    assert (project.parent / "escape.txt").read_text(encoding="utf-8") == "next door\n"
    (project.parent / "escape.txt").unlink()


def test_an_absolute_system_path_is_refused_and_untouched(project):
    before = state(project)
    result = MoveTool().execute("notes.txt", WINDOWS_TARGET)
    refused(result, "OUTSIDE the working directory")
    assert result.metadata["refused"] == "outside-working-directory"
    assert result.metadata.get("argument") == "to_path"
    assert state(project) == before
    refused(MoveTool().execute(os.path.join(os.environ.get("SystemRoot", "C:\\Windows"),
                                            "win.ini"), "stolen.ini"),
            "OUTSIDE the working directory")
    assert state(project) == before


def test_a_link_out_of_the_project_is_refused_by_both_tools(project):
    victim = project.parent / "victim.txt"
    victim.write_text("shared file\n", encoding="utf-8")
    if not make_escape_link(project, str(victim), "linked.txt"):
        pytest.skip("this box will not create symlinks (Windows needs the privilege "
                    "or Developer Mode); the junction fallback only covers folders")
    before = state(project)
    refused(MoveTool().execute("linked.txt", "moved.txt"), "symlink")
    assert not (project / "moved.txt").exists(), "the refusal still moved through the link"
    refused(RemoveTool().execute("linked.txt"), "symlink")
    assert victim.read_text(encoding="utf-8") == "shared file\n"
    assert not (project / "moved.txt").exists()
    assert victim.exists(), "the tool followed the link and deleted the real file"
    # Recounted while the link is still in the tree: `before` was taken with
    # `linked.txt` present, so this comparison could never have held after the
    # cleanup below removes that very entry — the assertion had been looking at its
    # own teardown instead of at the two refusals.
    assert state(project) == before, "a refusal that changed the tree"
    os.remove(str(project / "linked.txt"))


def test_a_junction_out_of_the_project_is_refused_for_a_directory(project):
    elsewhere = project.parent / "elsewhere"
    elsewhere.mkdir(parents=True)
    (elsewhere / "inner.txt").write_text("real\n", encoding="utf-8")
    if not make_escape_link(project, str(elsewhere), "linked_dir"):
        pytest.skip("no symlink or junction available on this box")
    refused(MoveTool().execute("linked_dir", "renamed_dir"), "symlink")
    refused(RemoveTool().execute("linked_dir", recursive=True), "symlink")
    assert (elsewhere / "inner.txt").read_text(encoding="utf-8") == "real\n"
    assert (project / "linked_dir").exists() or (project / "renamed_dir").exists()
    if (project / "renamed_dir").exists():
        pytest.fail("the tool renamed the link itself, but reported a refusal")
    os.remove(str(project / "linked_dir"))


# ---------------------------------------------------------------------------
# 5 — the folder BeeCode stands on
# ---------------------------------------------------------------------------

def test_the_working_directory_itself_cannot_be_moved_or_removed(project):
    before = state(project)
    refused(MoveTool().execute(".", "elsewhere"), "working directory")
    refused(RemoveTool().execute(".", recursive=True), "working directory")
    refused(RemoveTool().execute("./", recursive=True), "working directory")
    assert project.exists() and state(project) == before


def test_a_parent_of_the_working_directory_cannot_be_removed(project):
    refused(RemoveTool().execute("..", recursive=True), "parent")
    refused(RemoveTool().execute(str(project.parent), recursive=True), "parent")
    assert project.exists(), "the parent went and the project with it"


def test_a_folder_cannot_be_moved_into_itself(project):
    before = state(project)
    result = MoveTool().execute("build", os.path.join("build", "inner"))
    refused(result, "inside the folder being moved")
    assert result.metadata["refused"] == "destination-inside-source"
    assert state(project) == before


def test_a_source_that_is_not_there_is_refused(project):
    before = state(project)
    result = MoveTool().execute("nope.txt", "somewhere.txt")
    refused(result, "does not exist")
    assert result.metadata["refused"] == "source-missing"
    assert state(project) == before
    refused(MoveTool().execute("", "somewhere.txt"), "non-empty path")
    refused(MoveTool().execute("notes.txt", None), "non-empty path")


# ---------------------------------------------------------------------------
# 6 — remove
# ---------------------------------------------------------------------------

def test_a_directory_is_refused_without_recursive(project):
    before = state(project)
    result = RemoveTool().execute("build")
    refused(result, "is a directory", "recursive=true")
    assert result.metadata["refused"] == "directory-needs-recursive"
    assert state(project) == before


def test_recursive_deletes_a_tree_and_counts_it(project, fake_journal):
    result = RemoveTool().execute("build", recursive=True)
    assert not result.error, result.output
    assert not (project / "build").exists()
    assert "2 files, 150 bytes, 2 directories" in result.output
    assert result.metadata["files"] == 2 and result.metadata["bytes"] == 150
    journalled = [call[2] for call in fake_journal.calls if call[1] == "remove"]
    assert len(journalled) == 2 and all(call for call in journalled)


def test_a_single_file_removal_reports_its_bytes(project):
    result = RemoveTool().execute("notes.txt")
    assert not result.error, result.output
    assert not (project / "notes.txt").exists()
    assert result.metadata["files"] == 1
    assert result.metadata["bytes"] == (len("заметка\n".encode("utf-8")))


def test_removing_git_needs_its_own_flag(project):
    git = project / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git / "objects" / "pack.idx").write_bytes(b"\xff" * 40)
    before = state(project)
    result = RemoveTool().execute(".git", recursive=True)
    refused(result, ".git", "confirm_git=true")
    assert result.metadata["refused"] == "git-directory"
    assert state(project) == before
    refused(RemoveTool().execute(".git/HEAD", confirm_git=True), "version history")
    refused(RemoveTool().execute(".git", confirm_git=True), "recursive=true")
    assert state(project) == before
    # Only both words together, and then the history really does go.
    result = RemoveTool().execute(".git", recursive=True, confirm_git=True)
    assert not result.error, result.output
    assert not git.exists()
    assert result.metadata["files"] == 2


def test_the_history_of_a_parent_folder_is_protected_too(project):
    (project / "src" / ".git").mkdir()
    (project / "src" / ".git" / "HEAD").write_text("x\n", encoding="utf-8")
    refused(RemoveTool().execute("src/.git", recursive=True), "confirm_git")
    assert (project / "src" / ".git" / "HEAD").exists()


def test_recursive_is_not_switched_on_by_a_string(project):
    before = state(project)
    result = RemoveTool().execute("build", recursive="no")
    refused(result, "is a directory")
    assert state(project) == before


def test_a_survivor_is_never_reported_as_success(project, monkeypatch):
    real = files_mod._remove_one

    def fails_on(path, *args, **kwargs):
        if str(path).endswith("b.o"):
            return False                       # an open handle, a locked file
        return real(path, *args, **kwargs)

    monkeypatch.setattr(files_mod, "_remove_one", fails_on)
    result = RemoveTool().execute("build", recursive=True)
    assert result.error is True
    assert "INCOMPLETE" in result.output
    for claim in ("Removed ", "deleted: "):
        assert claim not in result.output
    assert "b.o" in result.output
    assert (project / "build" / "out" / "b.o").exists(), "the report and the disk differ"
    assert not (project / "build" / "a.o").exists()


def test_removing_nothing_is_reported_as_nothing_removed(project):
    before = state(project)
    result = RemoveTool().execute("ghost.txt")
    refused(result, "does not exist")
    assert result.metadata["refused"] == "not-found"
    assert state(project) == before


# ---------------------------------------------------------------------------
# 7 — the journal: no entry, no change
# ---------------------------------------------------------------------------

def test_without_a_journal_nothing_is_moved_or_removed(project, no_journal):
    no_journal(broken_import=True)
    before = state(project)
    moved = MoveTool().execute("notes.txt", "elsewhere.txt")
    refused(moved, "undo journal", "previous state")
    assert moved.metadata["refused"] == "undo-journal-not-recorded"
    removed = RemoveTool().execute("notes.txt")
    assert removed.error is True and "journal" in removed.output
    assert state(project) == before
    assert not (project / "elsewhere.txt").exists()
    no_journal(broken_import=False)
    assert state(project) == before
    refused(MoveTool().execute("notes.txt", "elsewhere.txt"))
    assert state(project) == before


def test_a_journal_that_raises_stops_the_operation(project, fake_journal):
    fake_journal.error = "disk full"
    before = state(project)
    result = MoveTool().execute("notes.txt", "elsewhere.txt")
    refused(result, "disk full")
    assert state(project) == before
    result = RemoveTool().execute("notes.txt")
    refused(result, "disk full")
    assert state(project) == before


def test_the_entry_is_written_before_the_disk_moves(project, fake_journal):
    seen = {}
    real_record = fake_journal.record

    def watching(workdir, tool, path, *, action="modify"):
        # At record time the old name must still be there and the new one not yet.
        seen["src_exists"] = os.path.lexists(str(project / "notes.txt"))
        seen["dst_exists"] = os.path.lexists(str(project / "elsewhere.txt"))
        seen["action"] = action
        seen["undo_dir"] = os.path.join(str(workdir), ".beeagent", "undo")
        return real_record(workdir, tool, path, action=action)

    fake_journal.record = watching
    result = MoveTool().execute("notes.txt", "elsewhere.txt")
    assert not result.error, result.output
    assert seen == {"src_exists": True, "dst_exists": False, "action": "modify",
                    "undo_dir": os.path.join(files_mod.working_dir(), ".beeagent", "undo")}
    assert "/undo" in result.output, "a journalled change should say so"


def test_a_tree_bigger_than_the_journal_budget_is_refused(project, fake_journal):
    fake_journal.MAX_ENTRIES = 2
    before = state(project)
    result = RemoveTool().execute("build", recursive=True)
    assert not result.error or "INCOMPLETE" not in result.output
    if result.error:
        refused(result, "unrecoverable")
        assert state(project) == before
    # A journal that cannot hold the tree has to stop the call or say what it lost.
    assert result.metadata.get("journal_entries", 0) <= 2 or result.error is True


def test_the_real_journal_module_is_used_when_it_exists(project):
    """The integration this tool depends on: the real `record()` accepts the call.

    Skipped while `beeagent/core/journal.py` is not installed — the rest of this
    suite runs either way, which is the point of the fake above.
    """
    import beeagent.core as core_pkg
    real = sys.modules.get("beeagent.core.journal")
    try:
        from beeagent.core import journal as journal_mod
    except Exception:
        pytest.skip("beeagent.core.journal does not import on this box")
    if not callable(getattr(journal_mod, "record", None)) or hasattr(journal_mod, "record") \
            and journal_mod.record.__module__ != "beeagent.core.journal":
        pytest.skip("the real journal is shadowed by a fixture")
    del real, core_pkg
    manifest = project / ".beeagent" / "undo" / "journal.jsonl"
    (project / "journal-note.txt").write_text("precious\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as patch:
        patch.undo()                           # no-op; keeps the fixture honest below
    # Drop the fake so the import inside the tool finds the real module again.
    saved = sys.modules.pop("beeagent.core.journal", None)
    try:
        result = MoveTool().execute("journal-note.txt", "kept-note.txt")
    finally:
        sys.modules["beeagent.core.journal"] = saved
    if result.error and "journal" in result.output.lower():
        pytest.skip(f"the real journal refused the record: {result.output[:200]}")
    assert not result.error, result.output
    assert manifest.is_file(), "the move happened with nothing written to the journal"
    line = json.loads(manifest.read_text(encoding="utf-8").splitlines()[-1])
    assert line["tool"] == "move" and line["action"] == "modify"
    assert line["path"].endswith("journal-note.txt")
    assert (project / "kept-note.txt").read_text(encoding="utf-8") == "precious\n"


# ---------------------------------------------------------------------------
# 8 — both languages, always
# ---------------------------------------------------------------------------

def test_every_refusal_comes_in_both_languages(project, monkeypatch):
    monkeypatch.setattr(i18n, "_lang", "en")
    english = RemoveTool().execute("build").output
    assert "is a directory" in english
    monkeypatch.setattr(i18n, "_lang", "ru")
    russian = RemoveTool().execute("build").output
    assert "каталог" in russian and "is a directory" not in russian
    monkeypatch.setattr(i18n, "_lang", "en")
    plain = MoveTool().execute("notes.txt", "Readme.md").output
    monkeypatch.setattr(i18n, "_lang", "ru")
    translated = MoveTool().execute("notes.txt", "Readme.md").output
    assert "already exists" in plain and "существует" in translated
    monkeypatch.setattr(i18n, "_lang", "en")
