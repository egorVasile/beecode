"""The undo journal: the way back from a write the user did not mean.

These tests are about BYTES, because the only promise the journal makes is that
the bytes which were there can be put back. So every case is driven for real —
the actual `write` and `edit` tools in the actual process cwd, the manifest read
off the disk, the file compared byte for byte — and one of them is a Russian file
with CRLF endings, which is the ordinary input for the users of this program and
the case a text-mode rewrite gets wrong.

Two failures these particular tests exist to prevent:

* an undo that silently recorded nothing: a refusal which reads as a result is
  the worst class of bug in this project, so a tool that cannot journal its
  change must not write it, and must say so;
* a journal that grows forever, or a half-written line that makes `/undo` crash
  on the one thing the user is reaching for in a panic.

Everything is under `tmp_path` — the journal writes into the project it belongs
to, and the litter guard in `conftest.py` is right to fail a test that writes
into the repository instead. Non-ASCII diagnostics come back as hex, because the
console on the development box is cp1251.
"""
import io
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import pytest

from beeagent import i18n
from beeagent.core import journal
from beeagent.tools.edit import EditTool
from beeagent.tools.write import WriteTool
from beeagent.ui.commands import COMMANDS, HANDLERS, ReplContext, dispatch

CYRILLIC_WITH_CRLF = "строка одна\r\nстрока два\r\n"


# --------------------------------------------------------------------------
# fixtures and helpers
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def project(tmp_path, monkeypatch):
    """The tools resolve against the process cwd, so give every test its own."""
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def outside(tmp_path):
    """A real directory no root of the policy covers: beside the temp tree.

    A neighbour of `tmp_path` is not it — the temp directory is a root BeeCode may
    write into, so proving containment needs a sibling of that root.
    """
    temp_root = Path(tempfile.gettempdir()).resolve()
    candidate = temp_root.parent / f"beecode-journal-outside-{os.getpid()}-{tmp_path.name}"
    try:
        candidate.mkdir(parents=True)
    except OSError:
        pytest.skip(f"cannot create a directory beside {temp_root}")
    (candidate / "victim.txt").write_bytes(b"do not touch\n")
    yield candidate
    shutil.rmtree(candidate, ignore_errors=True)


class FakeAgent:
    """Only `/undo` reads `ctx.agent.workdir`, and it reads nothing else."""

    def __init__(self, workdir):
        self.workdir = str(workdir)


@pytest.fixture
def command_ctx(project):
    """`/undo` in the shared machinery, and the registry put back afterwards.

    COMMANDS and HANDLERS are process globals: a registration left behind here
    would show up in the next test file's `/help`, and in the README-vs-registry
    comparison, which is a different agent's file to keep honest.
    """
    before_commands = list(COMMANDS)
    before_handlers = dict(HANDLERS)
    journal.register_command()
    yield ReplContext(agent=FakeAgent(project))
    COMMANDS[:] = before_commands
    HANDLERS.clear()
    HANDLERS.update(before_handlers)


def text(result) -> str:
    """The rendered body of a CommandResult, table or Text or plain string."""
    body = result.output
    if isinstance(body, str):
        return body
    buffer = io.StringIO()
    from rich.console import Console

    Console(file=buffer, width=200).print(body)
    return buffer.getvalue()


def table_cells(result_or_text) -> list[str]:
    """Every cell of a rendered listing, box drawing taken out.

    Asserting on the glyphs would pin one rich `box` style, and `/undo` borrows
    the frame the current skin asks for — the cells are the fact, the border is
    decoration.
    """
    body = result_or_text if isinstance(result_or_text, str) else text(result_or_text)
    return [cell.strip() for cell in re.split(r"[^0-9A-Za-zа-яА-Я.:_/\- ]+", body)
            if cell.strip()]


def manifest_lines(where: Path) -> list[str]:
    path = journal.undo_root(where) / journal.MANIFEST_NAME
    if not path.is_file():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def blobs(where: Path) -> list[str]:
    root = journal.undo_root(where)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.name.startswith(journal.BLOB_PREFIX))


def hexs(data: bytes) -> str:
    """Diagnostics the cp1251 console can print."""
    return data.decode("utf-8", "backslashreplace").encode("unicode_escape").decode("ascii")


def ts_is_recent(stamp) -> bool:
    """The timestamp is this run's, to the second."""
    return abs(stamp - time.time()) < 120


# ------------------------------------------------------------------ recording -

def test_record_stores_the_bytes_and_appends_one_line(project):
    (project / "a.txt").write_bytes(b"the bytes before\n")
    entry = journal.record(str(project), "write", "a.txt", action="modify")

    assert entry["seq"] == 1
    assert entry["tool"] == "write"
    assert entry["path"] == "a.txt"                  # as given, not rewritten
    assert entry["action"] == "modify"
    assert entry["blob"]
    assert ts_is_recent(entry["ts"])
    stored = journal.undo_root(project) / entry["blob"]
    assert stored.is_file() and stored.read_bytes() == b"the bytes before\n"
    line = json.loads(manifest_lines(project)[0])
    assert line == json.loads(json.dumps(entry))     # one line, exactly this entry


def test_record_refuses_an_action_it_does_not_know(project):
    (project / "a.txt").write_bytes(b"x")
    with pytest.raises(journal.JournalError):
        journal.record(str(project), "write", "a.txt", action="truncate")
    assert manifest_lines(project) == []
    assert blobs(project) == []


def test_record_reads_a_synonym_and_stores_the_canonical_action(project):
    """`create` and `remove` are the same three operations in other words."""
    (project / "r.txt").write_bytes(b"contents\n")
    created = journal.record(str(project), "files", "n.txt", action="create")
    removed = journal.record(str(project), "files", "r.txt", action="remove")
    assert (created["action"], removed["action"]) == ("new", "delete")
    # a payload of statistics is not an action, and guessing one is how an
    # unrecoverable change gets called recoverable
    (project / "m.txt").write_bytes(b"move me\n")
    with pytest.raises(journal.JournalError) as caught:
        journal.record(str(project), "move", "m.txt", action={"to": "other.txt"})
    assert "nothing was recorded" in str(caught.value)
    assert len(manifest_lines(project)) == 2


def test_recording_a_new_file_stores_no_blob_but_the_action(project):
    entry = journal.record(str(project), "write", "new.py", action="new")
    assert entry["action"] == "new" and entry["blob"] == "" and entry["size"] == 0
    assert blobs(project) == []
    # ... and a `"new"` for a file that is actually there becomes a snapshot:
    # undoing it must not delete bytes this session never wrote.
    (project / "here.txt").write_bytes(b"somebody else\n")
    upgraded = journal.record(str(project), "write", "here.txt", action="new")
    assert upgraded["action"] == "modify" and upgraded["blob"]
    assert blobs(project) == [upgraded["blob"]]


# ------------------------------------------------------------------- rolling --

def test_undo_puts_a_cyrillic_crlf_file_back_byte_for_byte(project):
    original = CYRILLIC_WITH_CRLF.encode("utf-8")
    (project / "платежи.txt").write_bytes(original)
    entry = journal.record(str(project), "edit", "платежи.txt", action="modify")
    (project / "платежи.txt").write_bytes("完".encode("utf-8") + b"\n")

    results = journal.undo(str(project), 1)

    assert [r["undone"] for r in results] == ["restored"], results
    got = (project / "платежи.txt").read_bytes()
    assert got == original, f"restored {hexs(got)} expected {hexs(original)}"
    assert entry["blob"] not in blobs(project), "the spent copy must be retired"
    assert manifest_lines(project) == []


def test_undo_of_a_new_file_deletes_it(project):
    journal.record(str(project), "write", "создан.py", action="new")
    (project / "создан.py").write_bytes(b"# created\n")
    results = journal.undo(str(project), 1)
    assert [r["undone"] for r in results] == ["deleted"]
    assert not (project / "создан.py").exists()


def test_undo_of_a_delete_record_restores_the_bytes(project):
    (project / "важное.txt").write_bytes("удали меня\n".encode("utf-8"))
    journal.record(str(project), "bash", "важное.txt", action="delete")
    (project / "важное.txt").unlink()
    results = journal.undo(str(project), 1)
    assert [r["undone"] for r in results] == ["restored"]
    assert (project / "важное.txt").read_bytes() == "удали меня\n".encode("utf-8")


def test_rolling_back_three_ops_lands_on_the_oldest_bytes(project):
    (project / "x.txt").write_bytes(b"v1\n")
    journal.record(str(project), "edit", "x.txt", action="modify")
    (project / "x.txt").write_bytes(b"v2\n")
    journal.record(str(project), "edit", "x.txt", action="modify")
    (project / "x.txt").write_bytes(b"v3\n")

    results = journal.undo(str(project), 2)

    assert [r["undone"] for r in results] == ["restored", "restored"]
    assert (project / "x.txt").read_bytes() == b"v1\n"
    assert len(manifest_lines(project)) == 0


def test_undo_counts_the_last_n_newest_first(project):
    (project / "one.txt").write_bytes(b"1\n")
    (project / "two.txt").write_bytes(b"2\n")
    journal.record(str(project), "edit", "one.txt", action="modify")
    journal.record(str(project), "edit", "two.txt", action="modify")
    (project / "one.txt").write_bytes(b"NEW\n")
    (project / "two.txt").write_bytes(b"NEW\n")

    results = journal.undo(str(project), 1)

    assert [(r["path"], r["undone"]) for r in results] == [("two.txt", "restored")]
    assert (project / "two.txt").read_bytes() == b"2\n"
    assert (project / "one.txt").read_bytes() == b"NEW\n"


def test_undo_without_a_journal_returns_nothing_instead_of_failing(project):
    assert journal.undo(str(project), 1) == []
    assert journal.entries(str(project)) == []


def test_a_file_that_moved_away_is_reported_not_invented(project):
    journal.record(str(project), "write", "призрак.txt", action="new")
    results = journal.undo(str(project), 1)
    assert [r["undone"] for r in results] == ["gone"]
    assert not (project / "призрак.txt").exists()
    assert manifest_lines(project) == []


# -------------------------------------------------------------------- caps ----

def test_the_entry_cap_retires_the_oldest_blobs(project, monkeypatch):
    monkeypatch.setattr(journal, "MAX_ENTRIES", 3)
    for i in range(6):
        (project / f"f{i}.txt").write_bytes(f"body {i}\n".encode("utf-8"))
        journal.record(str(project), "write", f"f{i}.txt", action="modify")

    listed = journal.entries(str(project), limit=20)
    assert [e["seq"] for e in listed] == [6, 5, 4]        # newest first
    assert len(manifest_lines(project)) == 3
    assert len(blobs(project)) == 3, blobs(project)
    assert "blob-000001" not in " ".join(blobs(project))
    assert journal.clear(str(project)) == 3


def test_the_byte_cap_retires_until_the_total_fits(project, monkeypatch):
    monkeypatch.setattr(journal, "MAX_TOTAL_BYTES", 100)
    sizes = []
    for i in range(4):
        body = b"x" * 40 + b"\n"
        sizes.append(len(body))
        (project / f"g{i}.txt").write_bytes(body)
        journal.record(str(project), "write", f"g{i}.txt", action="modify")
    assert len(blobs(project)) == 2                      # 2 * 41 fits, 3 does not
    assert len(manifest_lines(project)) == 2


def test_a_snapshot_bigger_than_the_whole_budget_stays_available(project, monkeypatch):
    """The caps bound the journal over time, not the copy the user is about to need."""
    monkeypatch.setattr(journal, "MAX_TOTAL_BYTES", 10)
    body = b"y" * 40
    (project / "big.txt").write_bytes(body)
    journal.record(str(project), "write", "big.txt", action="modify")
    assert blobs(project)
    (project / "big.txt").write_bytes(b"gone\n")
    assert [r["undone"] for r in journal.undo(str(project), 1)] == ["restored"]
    assert (project / "big.txt").read_bytes() == body


def test_a_file_over_the_snapshot_ceiling_is_refused_not_skipped(project, monkeypatch):
    monkeypatch.setattr(journal, "MAX_SNAPSHOT_BYTES", 8)
    (project / "huge.txt").write_bytes(b"z" * 40)
    with pytest.raises(journal.JournalError) as caught:
        journal.record(str(project), "write", "huge.txt", action="modify")
    assert "40" in str(caught.value)
    assert blobs(project) == [] and manifest_lines(project) == []


# ----------------------------------------------------------------- damaged ----

def test_a_damaged_manifest_line_is_skipped_with_a_warning_and_not_fatal(project):
    (project / "good.txt").write_bytes(b"precious\n")
    journal.record(str(project), "edit", "good.txt", action="modify")
    root = journal.undo_root(project)
    with open(root / journal.MANIFEST_NAME, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"seq": 99, "path": "torn\n')                        # torn line
        handle.write(json.dumps({"seq": 100, "ts": 1, "tool": "edit", "path": "good.txt",
                                 "target": str(project / "good.txt"), "action": "modify",
                                 "blob": "blob-missing.snap", "size": 8}) + "\n")
    (project / "good.txt").write_bytes(b"changed\n")

    results = journal.undo(str(project), 3)

    states = [r["undone"] for r in results]
    assert states.count("dropped") == 2, states          # both damaged lines reported
    assert "restored" in states
    assert (project / "good.txt").read_bytes() == b"precious\n"
    for damaged in [r for r in results if r["undone"] == "dropped"]:
        assert damaged.get("error"), f"a skip with no warning: {damaged}"
    # the damaged lines are gone, so the journal is usable again afterwards
    assert "torn" not in (root / journal.MANIFEST_NAME).read_text(encoding="utf-8")


def test_entries_flag_a_line_whose_blob_is_not_on_disk(project):
    (project / "a.txt").write_bytes(b"one\n")
    entry = journal.record(str(project), "edit", "a.txt", action="modify")
    (journal.undo_root(project) / entry["blob"]).unlink()
    listed = journal.entries(str(project))
    assert listed[0]["damaged"] is True
    assert "cannot" in journal._means(listed[0])


def test_a_manifest_naming_a_file_outside_the_journal_is_not_obeyed(project):
    """The index is read from a cloned folder, and `/undo` deletes what it names.

    `blob: "../keep.txt"` resolves to a file of the user's one level up, inside
    the project: a line pointing there, at an absolute path, at a stream or at the
    folder itself has to be treated as a missing copy — dropped, and above all not
    deleted.
    """
    keeper = project / "keep.txt"
    keeper.write_bytes(b"somebody else's file\n")
    root = journal.undo_root(project)
    root.mkdir(parents=True)
    for hostile in ("../keep.txt", str(keeper), "keep.txt:evil.snap", "..", ".hidden"):
        line = json.dumps({"seq": 1, "ts": 1, "tool": "edit", "path": "a.txt",
                           "target": str(project / "a.txt"), "action": "modify",
                           "blob": hostile, "size": 4})
        (root / journal.MANIFEST_NAME).write_text(line + "\n", encoding="utf-8")
        results = journal.undo(str(project), 1)
        assert [r["undone"] for r in results] == ["dropped"], hostile
        assert keeper.read_bytes() == b"somebody else's file\n", hostile
        assert keeper.exists() and journal.clear(str(project)) == 0
    assert keeper.read_bytes() == b"somebody else's file\n"


# ------------------------------------------------------------------ policy ----

def test_undo_refuses_a_symlink_and_leaves_the_link_alone(project):
    (project / "real.txt").write_bytes(b"the shared file\n")
    (project / "note.txt").write_bytes(b"mine\n")
    journal.record(str(project), "write", "note.txt", action="modify")
    (project / "note.txt").unlink()
    try:
        os.symlink(str(project / "real.txt"), str(project / "note.txt"))
    except OSError as e:
        pytest.skip(f"this box does not allow symlinks: {e}")

    results = journal.undo(str(project), 1)

    assert [r["undone"] for r in results] == ["refused"], results
    assert (project / "note.txt").is_symlink(), "the link itself was replaced"
    assert (project / "real.txt").read_bytes() == b"the shared file\n"
    assert len(manifest_lines(project)) == 1, "a refusal must stay undoable on purpose"


def test_undo_refuses_to_write_outside_the_working_roots(project, outside):
    victim = outside / "victim.txt"
    journal.record(str(project), "write", str(victim), action="modify")
    changed = b"changed by something else\n"
    victim.write_bytes(changed)

    results = journal.undo(str(project), 1)

    assert [r["undone"] for r in results] == ["refused"], results
    assert "OUTSIDE" in results[0]["error"] or "вне" in results[0]["error"]
    # the point of the refusal: the byte that are there now are still there
    assert victim.read_bytes() == changed
    assert len(manifest_lines(project)) == 1


# ------------------------------------------------------------------ the wire --

def test_write_records_before_it_writes_and_says_the_bytes_are_recoverable(project):
    (project / "note.txt").write_bytes(b"old bytes\n")
    result = WriteTool().execute(path="note.txt", content="new bytes\n")
    assert result.error is False, result.output
    assert "/undo" in result.output, result.output
    assert "previous bytes saved" in result.output
    listed = journal.entries(str(project))
    assert [(e["tool"], e["action"]) for e in listed] == [("write", "modify")]
    assert listed[0]["path"] == "note.txt"
    journal.undo(str(project), 1)
    assert (project / "note.txt").read_bytes() == b"old bytes\n"


def test_write_of_a_new_file_is_recorded_as_new_and_undo_removes_it(project):
    result = WriteTool().execute(path="новый.py", content="x = 1\n")
    assert result.error is False, result.output
    assert "new file" in result.output
    assert journal.entries(str(project))[0]["action"] == "new"
    journal.undo(str(project), 1)
    assert not (project / "новый.py").exists()


def test_edit_records_modify_and_undo_round_trips_crlf(project):
    original = ("def charge():\r\n    return 1\r\n").encode("utf-8")
    (project / "orders.py").write_bytes(original)
    result = EditTool().execute(path="orders.py", old_text="    return 1",
                                new_text="    return 2")
    assert result.error is False, result.output
    assert "/undo" in result.output
    after = (project / "orders.py").read_bytes()
    assert after != original and b"\r\n" in after, hexs(after)
    journal.undo(str(project), 1)
    assert (project / "orders.py").read_bytes() == original, hexs(
        (project / "orders.py").read_bytes())


def test_the_recoverable_clause_is_in_both_languages(project):
    (project / "d.txt").write_bytes(b"1\n")
    english = WriteTool().execute(path="d.txt", content="2\n").output
    assert "previous bytes saved" in english
    i18n.set_lang("ru")
    try:
        russian = WriteTool().execute(path="d.txt", content="3\n").output
    finally:
        i18n.set_lang("en")
    assert "прежние байты сохранены" in russian, hexs(russian.encode("utf-8"))


def test_a_write_that_cannot_be_journalled_is_refused_and_writes_nothing(project):
    """.beeagent is a file, so the journal cannot be created — and `write` has to
    stop rather than change a byte it can never put back."""
    (project / ".beeagent").write_bytes(b"not a directory\n")
    (project / "keep.txt").write_bytes(b"the user's own\n")

    over = WriteTool().execute(path="keep.txt", content="overwritten\n")
    created = WriteTool().execute(path="brand-new.txt", content="created\n")
    edited = EditTool().execute(path="keep.txt", old_text="user", new_text="model")

    for result in (over, created, edited):
        assert result.error is True, f"a refusal that reads as success: {result.output}"
        assert "Written " not in result.output and "Replaced in" not in result.output
        assert "Записано" not in result.output
        assert result.metadata.get("refused") == "undo-journal-not-recorded"
    assert (project / "keep.txt").read_bytes() == b"the user's own\n"
    assert not (project / "brand-new.txt").exists()


def test_a_refused_write_records_nothing(project, outside):
    result = WriteTool().execute(path=str(outside / "victim.txt"), content="x\n")
    assert result.error is True
    assert journal.entries(str(project)) == []
    assert blobs(project) == []
    assert manifest_lines(project) == []


# ------------------------------------------------------------- the real path --

def test_what_the_user_types_to_get_a_file_back(command_ctx, project):
    """The whole user story, driven through the tools and the command."""
    original = "свой текст\r\nвторой\r\n".encode("utf-8")
    (project / "README.md").write_bytes(original)
    assert WriteTool().execute(path="README.md", content="модель написала это\n").error is False

    listing = table_cells(dispatch(command_ctx, "/undo"))
    assert "README.md" in listing and "write" in listing
    assert (project / "README.md").read_bytes() != original, "the listing wrote to the file"

    body = text(dispatch(command_ctx, "/undo 1"))
    assert "restored" in body, body
    assert (project / "README.md").read_bytes() == original, hexs(
        (project / "README.md").read_bytes())


# ------------------------------------------------------------------ /undo -----

def test_register_command_adds_undo_once_and_survives_a_second_call(command_ctx):
    assert "undo" in {c.name for c in COMMANDS}
    assert COMMANDS.count(next(c for c in COMMANDS if c.name == "undo")) == 1
    assert journal.register_command() is True
    assert [c.name for c in COMMANDS].count("undo") == 1
    assert HANDLERS["undo"] is journal._cmd_undo
    assert "undo" in text(dispatch(command_ctx, "/help")).lower()


def test_undo_without_arguments_prints_the_journal(command_ctx, project):
    (project / "a.txt").write_bytes(b"first\n")
    journal.record(str(project), "edit", "a.txt", action="modify")
    journal.record(str(project), "write", "b.txt", action="new")

    cells = table_cells(text(dispatch(command_ctx, "/undo")))

    # sequence, time, tool, path, action — the five the user reads before typing
    assert "2" in cells and "1" in cells, cells
    assert "edit" in cells and "write" in cells, cells
    assert "a.txt" in cells and "b.txt" in cells, cells
    assert any(re.match(r"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d", c) for c in cells), cells
    assert "restore the bytes it replaced" in cells, cells
    assert "delete the file it created" in cells, cells
    assert "/undo <n>" in text(dispatch(command_ctx, "/undo"))
    # the listing changed nothing
    assert (project / "a.txt").read_bytes() == b"first\n"
    assert len(manifest_lines(project)) == 2


def test_undo_with_a_number_restores_that_many(command_ctx, project):
    (project / "a.txt").write_bytes(b"one\n")
    (project / "b.txt").write_bytes(b"two\n")
    journal.record(str(project), "edit", "a.txt", action="modify")
    journal.record(str(project), "edit", "b.txt", action="modify")
    (project / "a.txt").write_bytes(b"UNO\n")
    (project / "b.txt").write_bytes(b"DOS\n")

    body = text(dispatch(command_ctx, "/undo 2"))

    assert (project / "a.txt").read_bytes() == b"one\n", hexs(
        (project / "a.txt").read_bytes())
    assert (project / "b.txt").read_bytes() == b"two\n"
    assert "restored" in body
    assert manifest_lines(project) == []


def test_undo_clear_empties_the_journal_and_its_blobs(command_ctx, project):
    (project / "a.txt").write_bytes(b"one\n")
    journal.record(str(project), "edit", "a.txt", action="modify")
    assert blobs(project)
    body = text(dispatch(command_ctx, "/undo clear"))
    assert "empty" in body.lower(), body
    assert manifest_lines(project) == [] and blobs(project) == []


def test_undo_says_so_when_there_is_nothing_recorded(command_ctx, project):
    body = text(dispatch(command_ctx, "/undo"))
    assert "nothing is recorded" in body
    body = text(dispatch(command_ctx, "/undo 2"))
    assert "nothing to undo" in body


def test_a_bad_argument_shows_the_usage(command_ctx):
    assert "usage: /undo" in text(dispatch(command_ctx, "/undo sideways"))
