"""`move` and `remove` — renaming and deleting as two named operations.

Both existed only through `bash`, and `bash` is one blanket grant covering
everything a shell can do: a user who wants the model to be able to rename a file
has to allow `rm -rf`, `curl` and whatever else fits on a command line. Two narrow
tools make the permission gate mean something (`/allow move` is exactly renaming)
and the audit trail real (one resolved pair per call, not a shell history line).

Every refusal below names a way a guessed argument destroys work, because a free
endpoint's `to_path` can name a file the user cares about, `../` can point outside
the project and a symlink can point anywhere. Containment is
:func:`beeagent.tools._path_policy.guard`'s — the one rule in this project,
computed from `realpath` — and no second rule about where a path may go is
invented here. What these two add on top is the state of the session itself: never
move or delete the folder we stand in, never change a byte the journal has not
taken, and never report a result the disk does not show.

The journal, and what it can and cannot hold
-------------------------------------------
`beeagent.core.journal.record(workdir, tool, path, action=...)` stores the bytes at
one **file**; a directory raises `JournalError` there, and a rename changes no
bytes at all. So the unit of coverage here is a regular file, and both tools record
one entry per file inside what they are about to change — `modify` for a move (undo
puts the bytes back under the old name), `delete` for a removal (undo puts them back
where they were and recreates the parent folders). A tree bigger than the journal's
own budget is refused rather than half-journalled: `/undo` restoring 50 of 5000
files while the transcript says "removed" is the lie this project hunts. Symlinks,
sockets and the directory skeleton carry no bytes to keep, so they are moved or
deleted with the tree and named in the report as not recoverable.

Windows and case — the one thing not to get wrong here
------------------------------------------------------
`guard` returns `realpath`'d paths, and on Windows `realpath` rewrites every
existing component to its **stored** case. `Readme.md` asked for as `README.md`
therefore resolves to `Readme.md`: the resolved destination equals the resolved
source, and the datum this tool exists to act on is gone from the resolved pair. So
the requested *spelling* is taken from the caller's own argument (absolute, never
realpath'd) while the resolved path decides where the bytes go. A case-only change
is done in two steps through a sibling staging name, and the outcome is read back
from `os.listdir` — if the folder still lists the old spelling the tool says Windows
swallowed it, puts the name back, and reports a refusal instead of a move that never
happened.
"""
from __future__ import annotations

import errno
import os
import re
import shutil
import stat
import tempfile

from beeagent.i18n import L

from . import _path_policy
from ._path_policy import guard, working_dir
from .base import BaseTool, ToolResult

_SEPARATORS = re.compile(r"[\\/]+")
# Only these spellings switch a flag on. A loosely-parsed call can hand us a
# string, and `"false"` is truthy in Python: `overwrite="false"` must not
# overwrite, and `recursive="no"` must not delete a tree.
_YES = ("1", "true", "yes", "y", "on", "да")
# The journal's own caps, with the values it ships with as the fallback.
_DEFAULT_LIMITS = {"MAX_ENTRIES": 50, "MAX_TOTAL_BYTES": 25 * 1024 * 1024,
                   "MAX_SNAPSHOT_BYTES": 64 * 1024 * 1024}


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------

def _refuse(message: str, tag: str, **metadata) -> ToolResult:
    """A refusal is an error the model sees, never a result that reads as success."""
    return ToolResult(output=message, error=True,
                      metadata={"refused": tag, **metadata})


def _fold(path) -> str:
    """The spelling a case-insensitive filesystem cannot tell apart."""
    text = os.path.normcase(str(path))
    return text.rstrip("\\/") or text


def _spelled(raw) -> str:
    """The path as it was written, absolute but NOT realpath'd.

    `realpath` normalises case on Windows, which is the one piece of information a
    case-only rename needs; `guard`'s result still owns containment.
    """
    return os.path.abspath(os.path.expanduser(str(raw)))


def _as_flag(value, default: bool = False) -> bool:
    """A model's `true`, `"true"`, `1` — and nothing else."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() in _YES
    return bool(value)


def _is_link(raw) -> bool:
    """Whether the name itself is a link, before `realpath` hides that fact."""
    try:
        return os.path.islink(_spelled(raw))
    except OSError:
        return False


def _leaf_of(raw, fallback: str) -> str:
    leaf = os.path.basename(_spelled(raw).rstrip("\\/"))
    return leaf if leaf and leaf not in (".", "..") else fallback


def _link_refusal(raw, verb: str) -> str:
    """Refuse a link named directly, whichever way it points.

    `guard` resolves the link, so the operation would run on the file at the far
    end while the transcript names the link: `remove notes.txt` would delete
    `big/notes.txt` and leave a dangling `notes.txt` behind — the same shape
    `beeagent.tools.base.reject_symlink_target` refuses on a write, and the same
    one `journal._inside_policy` refuses on an undo. A link that points OUT of the
    roots is refused by `guard` before this ever runs.
    """
    if not _is_link(raw):
        return ""
    real = os.path.realpath(_spelled(raw))
    return L(
        f"`{verb}` refused: `{raw}` is itself a symlink to `{real}`. Moving or "
        f"deleting the link and moving or deleting the file behind it are two "
        f"different things, and this argument does not say which one is meant. "
        f"Nothing was changed. Name the real file, or use `bash` deliberately when "
        f"the link itself is what should move.",
        f"`{verb}` отказал: `{raw}` — сама символьная ссылка на `{real}`. Тронуть "
        f"ссылку или тронуть файл за ней — два разных дела, и из аргумента не "
        f"видеть, что имеется в виду. Ничего не изменено. Назови настоящий файл; "
        f"если нужна именно ссылка — осознанный `bash`.")


def _root_paths() -> list[str]:
    """The roots themselves, from the policy's own computation.

    Containment is `guard`'s decision and stays there. This answers a different
    question — "is this directory the ground the session stands on?" — and it must
    not grow a second definition of what a root is, so it calls the policy's
    function and only falls back to the two roots that always exist.
    """
    computed = getattr(_path_policy, "_roots", None)
    if callable(computed):
        try:
            return [str(r) for r in computed()]
        except Exception:                            # noqa: BLE001 - absence is the answer
            pass
    return [_fold(os.path.realpath(os.getcwd())),
            _fold(os.path.realpath(tempfile.gettempdir()))]


def _anchor_refusal(target, verb: str) -> str:
    """Refuse to move or delete a directory the session is standing inside.

    `guard` cannot answer this one: a root is inside itself by construction, so the
    working directory and the interpreter's temp directory — where every fixture of
    this project's own suite lives — pass containment.
    """
    here = _fold(os.path.realpath(str(target)))
    workdir = working_dir()
    if here == workdir:
        return L(
            f"`{verb}` refused: `{target}` IS the working directory BeeCode was "
            f"started in. Every path after this call resolves against a folder that "
            f"would no longer be where it is. Nothing was changed.",
            f"`{verb}` отказал: `{target}` — это и есть рабочая папка, из которой "
            f"запустили BeeCode. Все пути после этого вызова будут искаться в папке, "
            f"которая исчезла с привычного места. Ничего не изменено.")
    for root in _root_paths():
        if here == root or str(root).startswith(here + os.sep):
            return L(
                f"`{verb}` refused: `{target}` is a parent of the folders BeeCode is "
                f"confined to (`{root}`, the working directory among them). Removing "
                f"or renaming it takes that whole area with it. Nothing was changed.",
                f"`{verb}` отказал: `{target}` — предок папок, в которых BeeCode "
                f"разрешено работать (здесь `{root}`; рабочая папка среди них). "
                f"Удалить или переименовать её — значит забрать всю область. Ничего "
                f"не изменено.")
    return ""


def _stored_name(directory, name: str) -> str:
    """The spelling `directory` really holds for `name` ("" when it holds none).

    `os.path.exists` cannot answer this: on a case-insensitive filesystem it says
    yes to both spellings whichever one is on disk. Only the listing tells the
    truth, so every post-condition check goes through it.
    """
    try:
        listed = os.listdir(str(directory))
    except OSError:
        return ""
    if name in listed:
        return name
    for item in listed:
        if _fold(item) == _fold(name):
            return item
    return ""


def _holds_exact(directory, name: str) -> bool:
    """Whether `directory` holds an entry spelled exactly `name`."""
    try:
        return name in os.listdir(str(directory))
    except OSError:
        return False


def _scan(target) -> tuple[list[tuple[str, int]], int]:
    """([ (regular file, bytes) ], directory count) under `target`.

    The file list is the journal's unit of coverage: a symlink, a socket or a
    directory carries no bytes to hand back, so it is deliberately not offered to
    `record()` — which would follow a link out of the tree on a snapshot and write
    through it on an undo.
    """
    path = str(target)
    if not os.path.isdir(path):
        return [(path, _bytes_of(path))], 0
    found: list[tuple[str, int]] = []
    dirs = 1
    for root, names, entries in os.walk(path):
        dirs += len(names)
        for name in entries:
            victim = os.path.join(root, name)
            try:
                info = os.stat(victim, follow_symlinks=False)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append((victim, info.st_size))
    return found, dirs


def _bytes_of(path: str) -> int:
    try:
        return os.stat(path, follow_symlinks=False).st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# the undo journal: guarded import, and a hard dependency
# ---------------------------------------------------------------------------

def journal_module():
    """`beeagent.core.journal`, or None when there is no undo journal.

    The import is guarded because the journal may not be installed, may not import
    (a broken optional dependency inside it) or may not exist yet. All three answer
    the same way, and it is not "go ahead": a change whose previous state cannot be
    recorded is a change this tool will not make.
    """
    try:
        from beeagent.core import journal
    except Exception:                                # noqa: BLE001 - absence is the answer
        return None
    return journal if callable(getattr(journal, "record", None)) else None


def _no_journal(tool: str, error=None) -> str:
    """The text of a refusal that is not allowed to read like a result."""
    journal = journal_module()
    if journal is not None and callable(getattr(journal, "refusal", None)):
        return journal.refusal(tool, error if error is not None else L(
            "there is no undo journal installed (`beeagent/core/journal.py` does "
            "not load), so the previous state is not recoverable",
            "журнала отмены нет (`beeagent/core/journal.py` не загружается), "
            "прежнее состояние не восстановить"))
    return L(
        f"`{tool}` refused: BeeCode has no undo journal to record the previous state "
        f"in, so it will not make the change. Without an entry there is nothing to "
        f"recover from, and a wrong guess by the model would cost the user work that "
        f"is not retrievable. Nothing was changed. "
        f"{'(' + str(error) + ') ' if error is not None else ''}"
        f"`beeagent.core.journal` has to be importable and its `record()` has to "
        f"accept this call.",
        f"`{tool}` отказал: журнала отмены, куда BeeCode записал бы прежнее состояние, "
        f"нет — и потому изменение не вносится. Записи нет — возвращать не к чему, а "
        f"неверная догадка модели стоит пользователю работы, которую не вернуть. "
        f"Ничего не изменено. {'(' + str(error) + ') ' if error is not None else ''}"
        f"`beeagent.core.journal` должен импортироваться, а его `record()` — "
        f"принимать этот вызов.")


def _limits(journal) -> dict:
    out = {}
    for name, fallback in _DEFAULT_LIMITS.items():
        try:
            out[name] = int(getattr(journal, name, fallback) or fallback)
        except (TypeError, ValueError):
            out[name] = fallback
    return out


def _over_budget(journal, files: list[tuple[str, int]]) -> str:
    """Refuse a tree the journal would silently fail to hold.

    `record()` snapshots one file at a time and `_trim()` retires the oldest lines
    past `MAX_ENTRIES`/`MAX_TOTAL_BYTES`, so recording every file of a big tree
    would leave the last 50 recoverable and the rest gone while the transcript
    promised otherwise. Checking the whole set first is what makes the promise true.
    """
    limits = _limits(journal)
    total = sum(size for _path, size in files)
    biggest = max((size for _path, size in files), default=0)
    if len(files) > limits["MAX_ENTRIES"]:
        return L(f"the journal keeps {limits['MAX_ENTRIES']} entries and this holds "
                 f"{len(files)} files, so most of them would be unrecoverable",
                 f"журнал хранит {limits['MAX_ENTRIES']} записей, а здесь файлов "
                 f"{len(files)}: большая часть была бы невосстановима")
    if total > limits["MAX_TOTAL_BYTES"]:
        return L(f"the journal holds {limits['MAX_TOTAL_BYTES']} bytes in total and "
                 f"this is {total} bytes, so most of it would be unrecoverable",
                 f"журнал вмещает {limits['MAX_TOTAL_BYTES']} байт, а здесь "
                 f"{total}: большая часть была бы невосстановима")
    if biggest > limits["MAX_SNAPSHOT_BYTES"]:
        return L(f"one of the files is {biggest} bytes and the journal snapshots at "
                 f"most {limits['MAX_SNAPSHOT_BYTES']}",
                 f"один из файлов весит {biggest} байт, а журнал сохраняет не более "
                 f"{limits['MAX_SNAPSHOT_BYTES']}")
    return ""


def _journal(tool: str, files: list[tuple[str, int]], action: str) -> tuple[list, str]:
    """Record every file about to change, BEFORE any of them changes.

    Returns (recorded entries, refusal). The refusal path is reached before the
    disk is touched, so a refusal really means "nothing happened".
    """
    journal = journal_module()
    if journal is None:
        return [], _no_journal(tool)
    if not files:
        return [], ""                    # no regular file, so no bytes at risk
    problem = _over_budget(journal, files)
    if problem:
        return [], _no_journal(tool, problem)
    recorded: list = []
    for path, _size in files:
        try:
            outcome = journal.record(working_dir(), tool, str(path), action=action)
        except Exception as e:                       # noqa: BLE001 - refused either way
            return [], _no_journal(tool, e)
        if outcome is None or outcome is False:
            # A journal that says "not stored" has to be believed: it is the only
            # component that knows whether the previous bytes survived.
            return [], _no_journal(tool, L("journal.record() reported that it stored "
                                           "nothing", "journal.record() сообщил, что "
                                                           "ничего не сохранил"))
        recorded.append(outcome)
    return recorded, ""


def _recoverable(entries) -> str:
    """The clause that says what `/undo` can actually give back — no more."""
    if not entries:
        return L("nothing was recoverable by BeeCode here: no regular file's bytes "
                 "were at stake, so `/undo` holds no copy of the names, the folder "
                 "skeleton or any symlinks",
                 "здесь BeeCode нечего возвращать: байты обычных файлов не "
                 "затрагивались, поэтому в `/undo` нет ни копий, ни имён, ни "
                 "скелета папок, ни ссылок")
    journal = journal_module()
    if len(entries) == 1 and journal is not None:
        clause = getattr(journal, "recoverable_clause", None)
        if callable(clause):
            try:
                return str(clause(entries[0])).lstrip(" —")
            except Exception:                        # noqa: BLE001 - the note is not vital
                pass
    seqs = [entry.get("seq") for entry in entries if isinstance(entry, dict)
            and isinstance(entry.get("seq"), int)]
    where = (L(f"journal #{min(seqs)}-#{max(seqs)}", f"журнал #{min(seqs)}-#{max(seqs)}")
             if seqs else L("the journal", "журнал"))
    return L(f"{len(entries)} files' previous bytes are in {where}; `/undo` puts them "
             f"back where they were. Folder names, empty directories and symlinks do "
             f"not come back.",
             f"прежние байты {len(entries)} файлов в {where}; `/undo` вернёт их на "
             f"место. Названий папок, пустых каталогов и ссылок это не вернёт.")


# ---------------------------------------------------------------------------
# tool 1 — move
# ---------------------------------------------------------------------------

class MoveTool(BaseTool):
    name = "move"
    aliases = ("rename", "move_file", "mv")
    description = (
        "Rename or move one file OR directory inside the working directory; missing parent "
        "folders are created unless `create_dirs=false`. Refuses an existing destination unless "
        "`overwrite=true`, anything a symlink leads outside, the working directory itself, and a "
        "folder moved into itself. A case-only rename (`Readme.md` -> `README.md`) is done in two "
        "steps and verified. Use this instead of `bash mv`, so renaming does not need a shell."
    )
    parameters = {
        "type": "object",
        "properties": {
            "from_path": {"type": "string", "description": "Existing file or directory"},
            "to_path": {"type": "string",
                        "description": "New name, or new path; an existing directory here means "
                                       "move INTO that directory"},
            "create_dirs": {"type": "boolean", "default": True,
                            "description": "Create missing parent directories of `to_path`"},
            "overwrite": {"type": "boolean", "default": False,
                          "description": "Replace an existing destination; without it the call "
                                         "is refused and nothing moves"},
        },
        "required": ["from_path", "to_path"],
    }
    writes_files = True

    def execute(self, from_path: str, to_path: str, create_dirs: bool = True,
                overwrite: bool = False) -> ToolResult:
        try:
            return self._run(from_path, to_path, create_dirs, overwrite)
        except Exception as e:                       # noqa: BLE001 - reported, not raised
            return ToolResult(output=L(f"`move` failed: {e}", f"`move` не выполнен: {e}"),
                              error=True)

    def _run(self, from_path, to_path, create_dirs, overwrite):
        for label, value in (("from_path", from_path), ("to_path", to_path)):
            if not isinstance(value, str) or not value.strip():
                return _refuse(
                    L(f"`move` refused: `{label}` must be a non-empty path, got "
                      f"{value!r}. Nothing was changed.",
                      f"`move` отказал: `{label}` должен быть непустым путём, "
                      f"получено {value!r}. Ничего не изменено."), "empty-argument")
            refusal = _link_refusal(value, "move")
            if refusal:
                return _refuse(refusal, "symlink", argument=label)
        src, refusal = guard(from_path, "move")
        if refusal:
            return _refuse(refusal, "outside-working-directory", argument="from_path")
        dst, refusal = guard(to_path, "move")
        if refusal:
            return _refuse(refusal, "outside-working-directory", argument="to_path")
        if not src.exists():
            return _refuse(
                L(f"`move` refused: `{from_path}` does not exist (resolved: {src}), so "
                  f"there is nothing to move. Nothing was changed.",
                  f"`move` отказал: `{from_path}` не существует (разрешённый путь: "
                  f"{src}) — переносить нечего. Ничего не изменено."), "source-missing")
        problem = _anchor_refusal(src, "move")
        if problem:
            return _refuse(problem, "working-directory")

        src_is_dir = src.is_dir()
        # The destination as asked for: the parent from `guard` (so no symlink and
        # no `..` can move it), the leaf as spelled (so Windows has not already
        # rewritten its case — see the module docstring).
        leaf = _leaf_of(to_path, dst.name)
        want = dst.parent / leaf
        case_only = False
        if _fold(want) == _fold(src):
            if leaf == src.name:
                return _refuse(
                    L(f"`move` refused: `{from_path}` and `{to_path}` name the same "
                      f"entry (`{src}`). A folder moved onto itself is not a rename, "
                      f"and the folder BeeCode was started in is the last one that "
                      f"may be sent into itself. Nothing was changed.",
                      f"`move` отказал: `{from_path}` и `{to_path}` — одна и та же "
                      f"запись (`{src}`). Перенос папки в саму себя — не "
                      f"переименование, а папку, откуда запустили BeeCode, тем более "
                      f"нельзя нести в неё же. Ничего не изменено."), "move-onto-itself")
            case_only = True                         # `Readme.md` -> `README.md`
            dest = want
        elif os.path.isdir(str(dst)):
            dest = dst / src.name                    # "move it into that folder"
        else:
            dest = want
        # Only an existing destination can be ground the session stands on; when
        # the model named a folder to move into, the entry that gets replaced is
        # `dest`, not the folder.
        if os.path.lexists(str(dest)):
            problem = _anchor_refusal(dest, "move")
            if problem:
                return _refuse(problem, "working-directory")
        if src_is_dir and not case_only:
            folded_src, folded_dest = _fold(str(src)), _fold(str(dest))
            if folded_dest == folded_src or folded_dest.startswith(folded_src + os.sep):
                return _refuse(_inside_source(src, dest), "destination-inside-source")

        # In a case-only rename the source itself answers to `dest`, so "something
        # is already there" has to mean "another entry is in the way".
        replacing = os.path.lexists(str(dest)) and _fold(dest) != _fold(src)
        if replacing:
            if not _as_flag(overwrite):
                return _refuse(
                    L(f"`move` refused: `{dest}` already exists and `overwrite` was "
                      f"not set, so the bytes behind it would be gone. Nothing was "
                      f"changed. Re-send with `overwrite=true` only when the user "
                      f"means to replace that file.",
                      f"`move` отказал: `{dest}` уже существует, а `overwrite` не "
                      f"выставлен — байты за ним пропали бы. Ничего не изменено. "
                      f"Повтори с `overwrite=true`, только если пользователь правда "
                      f"хочет заменить этот файл."), "destination-exists")
            if os.path.isdir(str(dest)):
                entries = os.listdir(str(dest))
                if entries:
                    return _refuse(
                        L(f"`move` refused: the destination directory `{dest}` is not "
                          f"empty ({len(entries)} entries inside). Replacing a file is "
                          f"one thing; replacing a folder nobody named is another. "
                          f"Nothing was changed.",
                          f"`move` отказал: конечная папка `{dest}` не пуста (внутри "
                          f"{len(entries)} записей). Заменить файл — одно дело, "
                          f"затереть папку, которую не называли, — другое. Ничего не "
                          f"изменено."), "destination-not-empty")

        parent_missing = not os.path.isdir(str(dest.parent))
        if parent_missing and not _as_flag(create_dirs, True):
            return _refuse(
                L(f"`move` refused: the folder `{dest.parent}` does not exist and "
                  f"`create_dirs` is off. Nothing was changed.",
                  f"`move` отказал: папки `{dest.parent}` нет, а `create_dirs` "
                  f"выключен. Ничего не изменено."), "missing-parent")

        # Journal first: a change nothing can undo is not a change this tool makes.
        # The destination's bytes are at risk too when something is replaced.
        source_files = _scan(src)[0]
        at_risk = _scan_replaced(dest, source_files) if replacing else source_files
        recorded, refusal = _journal("move", at_risk, "modify")
        if refusal:
            return _refuse(refusal, "undo-journal-not-recorded")

        if replacing and os.path.isdir(str(dest)):
            try:                                     # the empty folder just checked
                os.rmdir(str(dest))
            except OSError as e:
                return _refuse(
                    L(f"`move` refused: the empty destination directory `{dest}` could "
                      f"not be cleared ({e}). `{src}` was left alone.",
                      f"`move` отказал: пустую конечную папку `{dest}` не удалось "
                      f"убрать ({e}). `{src}` осталась нетронутой."),
                    "destination-not-empty")
            replacing = False
        made = _makedirs(dest.parent) if parent_missing else []
        failure = _rename(src, dest, replacing, case_only)
        if failure:
            _undo_makedirs(made)
            return _refuse(failure, "rename-failed")

        landed = _stored_name(dest.parent, dest.name)
        if not landed:
            return _refuse(
                L(f"`move` did not happen: the folder `{dest.parent}` does not list "
                  f"`{dest.name}` at all. Nothing is lost, but this cannot be "
                  f"reported as a move — check what the folder holds now.",
                  f"`move` не выполнен: папка `{dest.parent}` вообще не содержит "
                  f"`{dest.name}`. Ничего не потеряно, но назвать это переносом "
                  f"нельзя — посмотри, что в папке теперь."), "not-on-disk")
        if landed != dest.name:
            if case_only:
                # The two steps ran and the filesystem still holds the old
                # spelling: undo our own work and say so.
                restored = _restore(os.path.join(str(dest.parent), landed), src)
                return _refuse(
                    L(f"`move` refused by the filesystem: Windows swallowed the case "
                      f"change — the entry is still stored as `{landed}`, not "
                      f"`{dest.name}`, even after the two-step rename. {restored}",
                      f"`move` не состоялся: Windows проглотила смену регистра — "
                      f"запись так и хранится как `{landed}`, а не `{dest.name}`, "
                      f"даже после двухшагового переименования. {restored}"),
                    "case-swallowed")
            return _refuse(
                L(f"`move` named something the filesystem did not keep: the entry is "
                  f"stored as `{landed}`, not the requested `{dest.name}`. Check the "
                  f"real spelling before anything refers to it.",
                  f"`move` назвал имя, которого файловая система не сохранила: запись "
                  f"хранится как `{landed}`, а не как `{dest.name}`. Проверь "
                  f"настоящее написание, прежде чем на него ссылаться."),
                "name-not-kept")

        old_still_there = _holds_exact(src.parent, src.name)
        source_bytes = sum(s for _p, s in source_files)
        lines = [L(f"Moved {from_path} -> {to_path}", f"Перенесено {from_path} -> {to_path}"),
                 L(f"resolved: {src} -> {dest}", f"разрешённые пути: {src} -> {dest}")]
        if case_only:
            lines.append(L(
                f"case-only rename on a case-insensitive filesystem: done in two "
                f"steps, and the folder really lists it as `{dest.name}` now",
                f"смена одного регистра на регистронезависимой файловой системе: "
                f"выполнена в два шага, и папка правда показывает её теперь как "
                f"`{dest.name}`"))
        if src_is_dir:
            after = _scan(dest)
            after_bytes = sum(s for _p, s in after[0])
            warning = ""
            if len(after[0]) != len(source_files) or after_bytes != source_bytes:
                warning = L(
                    f" — WARNING: {len(source_files)} files/{source_bytes} bytes went "
                    f"in and {len(after[0])} files/{after_bytes} bytes came out; check "
                    f"the tree",
                    f" — ВНИМАНИЕ: вошло {len(source_files)} файлов/{source_bytes} "
                    f"байт, вышло {len(after[0])} файлов/{after_bytes} байт; проверь "
                    f"дерево")
            lines.append(L(f"the folder carries {after[1]} directories and "
                           f"{len(after[0])} files, {after_bytes} bytes{warning}",
                           f"в папке каталогов: {after[1]}, файлов: {len(after[0])}, "
                           f"{after_bytes} байт{warning}"))
        lines.append(L("the old name is gone", "старое имя исчезло") if not old_still_there
                     else L(f"note: the old name `{src}` is still on disk beside the "
                            f"new one, so this was a copy, not a move",
                            f"заметь: старое имя `{src}` осталось рядом с новым — это "
                            f"копирование, а не перенос"))
        if case_only:
            lines.append(L(f"`/undo` can only give the bytes back, not the spelling: "
                           f"to change it back run move({dest}, {src})",
                           f"`/undo` вернёт байты, но не регистр: чтобы вернуть — "
                           f"move({dest}, {src})"))
        lines.append(_recoverable(recorded))
        return ToolResult(
            output="\n".join(line for line in lines if line), error=False,
            metadata={"from": str(src), "to": str(dest), "stored_as": landed,
                      "case_only": case_only, "files": len(source_files),
                      "bytes": source_bytes, "old_name_gone": not old_still_there,
                      "journal_entries": len(recorded)})

    def is_safe(self) -> bool:
        return False


def _scan_replaced(dest, already: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """The bytes behind a destination being overwritten, added to the source's."""
    if os.path.isdir(str(dest)):
        return already + _scan(dest)[0]
    return already + [(str(dest), _bytes_of(str(dest)))]


def _inside_source(src, dest) -> str:
    return L(
        f"`move` refused: the destination `{dest}` is inside the folder being moved "
        f"(`{src}`), which is a rename that contains itself. Move the folder beside "
        f"it instead. Nothing was changed.",
        f"`move` отказал: назначение `{dest}` лежит внутри переносимой папки "
        f"(`{src}`) — переименование, которое само себя включает. Перенеси папку "
        f"рядом. Ничего не изменено.")


def _makedirs(parent) -> list[str]:
    """Create `parent`; return the directories that did not exist beforehand."""
    made: list[str] = []
    missing: list[str] = []
    cursor = str(parent)
    while not os.path.isdir(cursor):
        missing.append(cursor)
        above = os.path.dirname(cursor)
        if above == cursor:
            break
        cursor = above
    for item in reversed(missing):
        try:
            os.mkdir(item)
        except OSError:
            break
        made.append(item)
    return made


def _undo_makedirs(made: list[str]) -> None:
    """Take back the folders created for a move that then failed."""
    for item in reversed(made):
        try:
            os.rmdir(item)
        except OSError:
            return                       # somebody is using it; stop removing


def _staging_name(directory) -> str:
    """A sibling the filesystem does not hold yet — step one of a case rename."""
    for attempt in range(64):
        candidate = os.path.join(str(directory), f".beecode-case{os.getpid()}{attempt}")
        if not os.path.lexists(candidate):
            return candidate
    return ""


def _cross_device(e: OSError) -> bool:
    if getattr(e, "errno", None) == errno.EXDEV:
        return True
    # ERROR_NOT_SAME_DEVICE / ERROR_BAD_COMMAND on a Windows volume switch.
    return os.name == "nt" and getattr(e, "winerror", 0) in (17, 21)


def _rename(src, dest, replacing: bool, case_only: bool) -> str:
    """The rename. "" on success, the refusal text when nothing moved."""
    if case_only:
        return _case_rename(src, dest)
    try:
        if replacing:
            os.replace(str(src), str(dest))        # os.rename refuses an existing name
        else:
            os.rename(str(src), str(dest))
        return ""
    except OSError as e:
        if _cross_device(e):
            try:
                shutil.move(str(src), str(dest))   # different volume: copy then delete
                return ""
            except OSError as e2:
                return L(f"`move` failed copying `{src}` to another device as "
                         f"`{dest}`: {e2}",
                         f"`move` не смог перенести `{src}` на другой диск как "
                         f"`{dest}`: {e2}")
        return L(f"`move` failed: `{src}` -> `{dest}`: {e}",
                 f"`move` не выполнен: `{src}` -> `{dest}`: {e}")


def _case_rename(src, dest) -> str:
    """`Readme.md` -> `README.md`, which a single rename cannot be trusted with.

    Renaming an entry onto its own case-variant is allowed on NTFS, but fails or
    silently keeps the old spelling on other case-insensitive filesystems and on
    some network mounts. Out to a sibling name and back with the wanted case works
    wherever renaming works at all — and the listing afterwards, not this function,
    decides whether the tool is allowed to say it moved.
    """
    staging = _staging_name(src.parent)
    if not staging:
        return L(f"`move` refused: no free staging name beside `{src}` for a "
                 f"case-only rename, so the two steps cannot be taken. Nothing was "
                 f"changed.",
                 f"`move` отказал: рядом с `{src}` не нашлось свободного имени для "
                 f"промежуточного шага, так что два шага не получаются. Ничего не "
                 f"изменено.")
    try:
        os.rename(str(src), staging)
    except OSError as e:
        return L(f"`move` refused: step one of the case-only rename failed ({e}); "
                 f"`{src}` is untouched.",
                 f"`move` отказал: первый шаг переименования регистра не удался "
                 f"({e}); `{src}` не тронута.")
    try:
        os.rename(staging, str(dest))
    except OSError as e:
        return L(f"`move` failed: step two of the case-only rename did not happen "
                 f"({e}). {_restore(staging, src)}",
                 f"`move` не выполнен: второй шаг переименования регистра не удался "
                 f"({e}). {_restore(staging, src)}")
    return ""


def _restore(landed_path, src) -> str:
    """Put a name back where it started, so a refusal really changes nothing."""
    try:
        os.rename(str(landed_path), str(src))
        return L("The original name is back, so nothing changed.",
                 "Прежнее имя возвращено — изменений нет.")
    except OSError as e:
        return L(f"Restoring `{src}` failed too ({e}); the entry is sitting at "
                 f"`{landed_path}`. Fix this by hand and do not retry the tool.",
                 f"Вернуть `{src}` тоже не удалось ({e}); запись лежит как "
                 f"`{landed_path}`. Почини вручную, инструмент не повторяй.")


# ---------------------------------------------------------------------------
# tool 2 — remove
# ---------------------------------------------------------------------------

class RemoveTool(BaseTool):
    name = "remove"
    aliases = ("delete", "remove_file", "rm")
    description = (
        "Delete one file inside the working directory, or a whole tree with `recursive=true`. "
        "Refuses the working directory and any parent of it, a directory without `recursive`, "
        "anything a symlink leads outside, and a `.git` folder unless `confirm_git=true`. "
        "Reports the files and bytes actually removed. Use this instead of `bash rm`, so "
        "deleting does not need a shell."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string",
                     "description": "File (or directory, with `recursive`) to delete"},
            "recursive": {"type": "boolean", "default": False,
                          "description": "Delete a directory and everything under it"},
            "confirm_git": {"type": "boolean", "default": False,
                            "description": "Required in addition to `recursive` for anything "
                                           "named .git: it throws away history"},
        },
        "required": ["path"],
    }
    writes_files = True

    def execute(self, path: str, recursive: bool = False,
                confirm_git: bool = False) -> ToolResult:
        try:
            return self._run(path, recursive, confirm_git)
        except Exception as e:                       # noqa: BLE001 - reported, not raised
            return ToolResult(output=L(f"`remove` failed: {e}", f"`remove` не выполнен: {e}"),
                              error=True)

    def _run(self, path, recursive, confirm_git):
        if not isinstance(path, str) or not path.strip():
            return _refuse(
                L("`remove` refused: `path` must be a non-empty path. Nothing was "
                  "deleted.",
                  "`remove` отказал: `path` должен быть непустым путём. Ничего не "
                  "удалено."), "empty-argument")
        refusal = _link_refusal(path, "remove")
        if refusal:
            return _refuse(refusal, "symlink")
        target, refusal = guard(path, "remove")
        if refusal:
            return _refuse(refusal, "outside-working-directory")
        if not os.path.lexists(str(target)):
            return _refuse(
                L(f"`remove` refused: `{path}` does not exist (resolved: {target}), so "
                  f"nothing was deleted. Check the exact spelling with `list_directory` "
                  f"before removing anything else.",
                  f"`remove` отказал: `{path}` не существует (разрешённый путь: "
                  f"{target}) — удалять было нечего. Проверь точное написание через "
                  f"`list_directory`, прежде чем удалять ещё что-нибудь."), "not-found")
        problem = _anchor_refusal(target, "remove")
        if problem:
            return _refuse(problem, "working-directory")
        git_component = _git_component(target)
        if git_component:
            if not _as_flag(confirm_git):
                return _refuse(
                    L(f"`remove` refused: `{target}` contains `{git_component}` — the "
                      f"version history, which neither BeeCode nor an undo entry "
                      f"brings back. Nothing was deleted. Send `recursive=true, "
                      f"confirm_git=true` only when the user has said to throw the "
                      f"repository away.",
                      f"`remove` отказал: путь `{target}` содержит `{git_component}` — "
                      f"историю версий, которую не вернёт ни BeeCode, ни отмена. "
                      f"Ничего не удалено. Пришли `recursive=true, confirm_git=true`, "
                      f"только если пользователь сам сказал выбросить репозиторий."),
                    "git-directory")
            if not _as_flag(recursive):
                if os.path.isdir(str(target)):
                    return _refuse(
                        L(f"`remove` refused: `{git_component}` is a directory, so "
                          f"`recursive=true` is needed beside `confirm_git` too.",
                          f"`remove` отказал: `{git_component}` — каталог, поэтому рядом "
                          f"с `confirm_git` нужен ещё и `recursive=true`."),
                        "directory-needs-recursive")
                # One entry of the history, not the history itself: "is a
                # directory" would be a statement about the folder, not about the
                # target, and the target is what the model named. The reason the
                # call stops is the version history, and the refusal says that,
                # names the resolved path and names both flags it wants.
                return _refuse(
                    L(f"`remove` refused: `{target}` is an entry of `{git_component}` — "
                      f"the version history, which neither BeeCode nor an undo entry "
                      f"brings back. Taking one file out of it is refused until "
                      f"`recursive=true` goes beside `confirm_git=true`, so the "
                      f"repository is thrown away as a decision, not a stray "
                      f"deletion. Nothing was deleted.",
                      f"`remove` отказал: `{target}` — запись внутри `{git_component}`, "
                      f"то есть история версий, которую не вернёт ни BeeCode, ни отмена. "
                      f"Вытаскивать из неё один файл нельзя, пока рядом с "
                      f"`confirm_git=true` не стоит `recursive=true`: репозиторий "
                      f"выбрасывают решением, а не случайным удалением. Ничего не "
                      f"удалено."),
                    "git-directory")
        is_dir = target.is_dir()
        if is_dir and not _as_flag(recursive):
            return _refuse(
                L(f"`remove` refused: `{target}` is a directory ({_preview(target)}). "
                  f"Pass `recursive=true` when the whole tree behind it is really "
                  f"meant. Nothing was deleted.",
                  f"`remove` отказал: `{target}` — каталог ({_preview(target)}). "
                  f"Пришли `recursive=true`, если правда нужна вся ветка за ним. "
                  f"Ничего не удалено."), "directory-needs-recursive")

        files, dirs = _scan(target)
        size = sum(s for _p, s in files)
        extra = _other_entries(target, files)     # links, sockets: gone, not restorable
        recorded, refusal = _journal("remove", files, "delete")
        if refusal:
            return _refuse(refusal, "undo-journal-not-recorded")

        survivors = _delete(target, is_dir)
        if survivors or os.path.lexists(str(target)):
            remaining = survivors or [str(target)]
            listed = "; ".join(remaining[:10])
            more = L("" if len(remaining) <= 10 else f" (and {len(remaining) - 10} more)",
                     "" if len(remaining) <= 10 else f" (и ещё {len(remaining) - 10})")
            return ToolResult(
                output=L(f"`remove` is INCOMPLETE: {len(files)} files ({size} bytes) "
                         f"were meant to go, and these are still on disk: "
                         f"{listed}{more}. That is not a success — what remains is the "
                         f"user's work. Clear the open handle or the permission and "
                         f"delete the rest, or say plainly that it is still there.",
                         f"`remove` выполнен НЕЦЕЛИКО: должно было уйти {len(files)} "
                         f"файлов ({size} байт), а это по-прежнему на диске: "
                         f"{listed}{more}. Успехом это не назовёшь: оставшееся чужая "
                         f"работа. Убери открытый файл или права и удали остальное, "
                         f"либо честно скажи, что оно на месте."),
                error=True,
                metadata={"refused": "incomplete", "files": len(files), "bytes": size,
                          "survivors": survivors})
        detail = L(f"{len(files)} files, {size} bytes, {dirs} directories",
                   f"{len(files)} файлов, {size} байт, каталогов: {dirs}") if is_dir else \
            L(f"{len(files)} file, {size} bytes", f"{len(files)} файл, {size} байт")
        lines = [L(f"Removed {path}", f"Удалено {path}"),
                 L(f"resolved: {target}", f"разрешённый путь: {target}"),
                 L(f"deleted: {detail}", f"удалено: {detail}")]
        if extra:
            lines.append(L(f"{extra} further names (symlinks, sockets: nothing with "
                           f"bytes of its own) went with them, and the journal holds "
                           f"no copy of those",
                           f"ещё {extra} имён (ссылки, сокеты — у них нет своих "
                           f"байтов) ушло вместе, и в журнале их копий нет"))
        lines.append(_recoverable(recorded))
        return ToolResult(
            output="\n".join(lines), error=False,
            metadata={"removed": str(target), "files": len(files), "bytes": size,
                      "directories": dirs if is_dir else 0,
                      "journal_entries": len(recorded)})

    def is_safe(self) -> bool:
        return False


def _git_component(target) -> str:
    """The `.git` inside this path: a repository directory, a submodule's gitdir,
    a `foo.git` bare clone, or the `gitdir:` pointer file of a worktree."""
    for part in _SEPARATORS.split(str(target)):
        if part == ".git" or part.endswith(".git"):
            return part
    return ""


def _preview(target) -> str:
    """How big the refused directory is, so the model can judge its own call."""
    try:
        entries = os.listdir(str(target))
    except OSError:
        return L("unreadable", "не читается")
    folders = sum(1 for name in entries
                  if os.path.isdir(os.path.join(str(target), name)))
    return L(f"{len(entries)} entries, {folders} of them folders",
             f"{len(entries)} записей, из них папок: {folders}")


def _other_entries(target, files: list[tuple[str, int]]) -> int:
    """The names in the tree the journal cannot hold — links, sockets, the unreadable.

    Counting them is the difference between "everything is recoverable" and what
    is actually true: these go with the tree and no bytes are stored for them.
    """
    held = {path for path, _size in files}
    extra = 0
    for root, names, entries in os.walk(str(target)):
        for name in names:               # a linked folder is not descended into
            if os.path.islink(os.path.join(root, name)):
                extra += 1
        for name in entries:             # a plain folder is counted in `dirs` instead
            if os.path.join(root, name) not in held:
                extra += 1
    return extra


def _delete(target, is_tree: bool) -> list[str]:
    """Remove `target`, returning the paths still on disk.

    Deepest first by hand rather than `shutil.rmtree`, which stops at the first
    failure and leaves half a tree deleted with no list of what survived — and
    naming the survivors is this tool's whole contract.
    """
    path = str(target)
    if not is_tree:
        return [] if _remove_one(path) else [path]
    plan_files: list[str] = []
    plan_dirs: list[str] = []
    for root, names, entries in os.walk(path):
        plan_dirs.append(root)
        plan_files.extend(os.path.join(root, name) for name in entries)
    survivors = [p for p in plan_files if not _remove_one(p)]
    # A longer path is always deeper in one tree, so this orders children first.
    for directory in sorted(plan_dirs, key=len, reverse=True):
        if os.path.lexists(directory) and not _remove_one(directory):
            survivors.append(directory)
    return survivors


def _remove_one(path: str) -> bool:
    """One deletion, retried once after clearing a Windows read-only bit."""
    for attempt in (0, 1):
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.remove(path)
            return True
        except FileNotFoundError:
            return True                             # somebody else removed it
        except PermissionError:
            if attempt:
                return False
            try:
                os.chmod(path, stat.S_IWRITE)
            except OSError:
                return False
        except OSError:
            return False
    return False
