"""The undo journal: the bytes that were on disk before BeeCode changed them.

A user who grants `write` is granting a tool that replaces a file in one call,
and until now there was no way back. This module is that way back: every file
tool calls :func:`record` *before* it changes a byte, and `/undo` puts the stored
bytes where they came from.

Layout, all of it under the project the agent was started in:

    <workdir>/.beeagent/undo/journal.jsonl   one JSON line per operation, oldest first
    <workdir>/.beeagent/undo/blob-000042-7.snap   the bytes that were there

The manifest line is written *after* the blob it names, so a death in the middle
leaves a blob nobody points at (harmless, cleaned by `/undo clear`) rather than a
line pointing at a blob that is not there (an undo that would lie). Both ends are
still checked: :func:`undo` skips a line whose blob is gone with a warning
instead of dying on it.

Nothing here is third-party: no compiler on Termux, no git on a plain box, so the
snapshot is a byte copy and the index is a JSONL file. Stdlib only, on purpose.

Failure is loud by contract. :func:`record` raises :class:`JournalError` when it
cannot store what is about to be replaced, and every file tool answers that with
a refusal — a safety net that silently did not catch anything is worse than no
safety net, because the user believes they have one.

Restoring goes through `beeagent.tools.base.write_text_preserving`, the same
temporary-file-then-rename path the tools write through, and containment through
`beeagent.tools._path_policy.guard` — the one policy in this project. A second
policy would be a second thing to get wrong.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from beeagent.i18n import L
from beeagent.tools import _path_policy
from beeagent.tools.base import write_text_preserving

#: Where the journal lives, relative to the working directory.
UNDO_DIR = ".beeagent/undo"
MANIFEST_NAME = "journal.jsonl"
BLOB_PREFIX = "blob-"
BLOB_SUFFIX = ".snap"

# The caps. A journal that grows without bound is a directory of stale copies of
# the user's own files that nobody asked for, and it eats the disk the project
# lives on. Both are counted from the sizes recorded at the time, and neither is
# allowed to retire the newest entry: the copy that is about to be needed is the
# one that must still be there.
MAX_ENTRIES = 50
MAX_TOTAL_BYTES = 25 * 1024 * 1024
# One file bigger than this is not snapshotted at all, and the tool that asked is
# refused rather than left with an undo that cannot restore 400 MB from RAM.
MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
# How many lines `/undo` shows before the user types a number.
PREVIEW_LIMIT = 10

ACTIONS = ("modify", "new", "delete")

# A stored copy's name, as read back out of the index: one plain file in the
# journal folder. No separator, no colon (an alternate data stream on Windows),
# no leading dot, so nothing a cloned manifest names can reach outside it.
_BLOB_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]*$")

# A tool that says `create` means `"new"` and a tool that says `remove` means
# `"delete"`, and taking the synonym costs nothing: the entry stored still speaks
# the three-word vocabulary `undo()` reads. A payload is not a synonym — a caller
# that hands this slot a dict of statistics is asked for the action by name,
# because guessing one from `{"to": "other/path"}` would be the silent kind of
# wrong, and an entry `undo()` cannot read is worse than no entry.
_ACTION_SYNONYMS = {
    "modify": "modify", "overwrite": "modify", "update": "modify", "replace": "modify",
    "edit": "modify", "change": "modify",
    "new": "new", "create": "new", "add": "new",
    "delete": "delete", "remove": "delete", "unlink": "delete", "rm": "delete",
}

# One lock per journal folder: two BeeCode sessions on one project, and the
# agent itself runs tools on more than one thread.
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


class JournalError(RuntimeError):
    """The journal could not store what was about to change.

    An exception and not a false-y return value, deliberately: `record()` hands
    back a dict on success, so `if not journal.record(...)` is the check a caller
    would write and it would never fire. A caller that forgets to catch this
    simply does not write the file, which is the safe direction to fail in.
    """


def _folder_lock(workdir) -> threading.Lock:
    key = os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(
        str(workdir or ".")))))
    with _LOCKS_GUARD:
        lock = _LOCKS.get(key)
        if lock is None:
            lock = _LOCKS[key] = threading.Lock()
        return lock


def undo_root(workdir) -> Path:
    """The journal directory for a project, without creating or checking it."""
    base = Path(os.path.abspath(os.path.expanduser(str(workdir or "."))))
    return base / UNDO_DIR


def _usable_undo_root(workdir, create: bool) -> Path:
    """The journal directory, contained and not a link, optionally created.

    `.beeagent` is inside the user's project, so a cloned folder can hand it to
    us as a symlink to somewhere else and every snapshot would then be written
    outside the roots the policy carries. The write tools refuse that shape for
    a target file; the journal refuses it for itself.
    """
    root = undo_root(workdir)
    _checked, refusal = _path_policy.guard(str(root), "undo journal")
    if refusal:
        raise JournalError(refusal)
    for part in (root.parent, root):
        if os.path.islink(str(part)):
            raise JournalError(L(
                f"{part} is a symlink, so the undo journal would be written "
                f"outside {root} — remove the link and let BeeCode create the "
                f"real folder",
                f"{part} — символьная ссылка: журнал отмены попал бы не в {root}. "
                f"Убери ссылку, чтобы BeeCode создал настоящую папку"))
    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise JournalError(L(
                f"the undo journal could not create {root} ({e.__class__.__name__}: {e})",
                f"журналу отмены не удалось создать {root} ({e.__class__.__name__}: {e})"))
    return root


def _name_of(workdir, path) -> str:
    """The path exactly as it was given, made absolute — links left alone."""
    raw = str(path or "")
    candidate = Path(os.path.expanduser(raw))
    if not candidate.is_absolute():
        candidate = Path(os.path.abspath(os.path.expanduser(str(workdir or ".")))) / candidate
    return os.path.abspath(str(candidate))


def _target_of(workdir, path) -> str:
    """The file a tool will really change: resolved the way `guard` resolves it.

    `write` and `edit` work on the `realpath` the policy hands back, so the
    journal has to name the same file or an undo would restore bytes beside the
    link instead of into the file that was changed.
    """
    try:
        return os.path.realpath(_name_of(workdir, path))
    except OSError:
        return _name_of(workdir, path)


# ---------------------------------------------------------------- recording --

def _normalise_action(action) -> str:
    """One of `ACTIONS`, or the loud reason the caller's word is not one."""
    word = action
    if isinstance(word, dict):
        for key in ("action", "kind", "what"):
            if isinstance(word.get(key), str):
                word = word[key]
                break
        else:
            raise JournalError(L(
                f"the undo journal takes `action` as one of {', '.join(ACTIONS)}, not "
                f"a payload of fields like {sorted(word)}: nothing was recorded, so "
                f"this change is not recoverable and must not be made",
                f"журнал отмены ждёт в `action` одно из {', '.join(ACTIONS)}, а не "
                f"набор полей вроде {sorted(word)}: ничего не записано, значит "
                f"изменение не вернуть — и делать его нельзя"))
    mapped = _ACTION_SYNONYMS.get(str(word).strip().lower())
    if mapped is None:
        raise JournalError(L(
            f"the undo journal records {', '.join(ACTIONS)}; `{word}` is not one of "
            f"them, so nothing was stored",
            f"журнал отмены записывает {', '.join(ACTIONS)}; `{word}` из них не "
            f"{len(ACTIONS)}, поэтому ничего не сохранено"))
    return mapped


def record(workdir: str, tool: str, path: str, *, action: str = "modify") -> dict:
    """Store what is on disk right now, and append one line to the manifest.

    Call this BEFORE the bytes change. `action` says what the tool is about to
    do: `"modify"` — the file exists and is being rewritten, so snapshot it;
    `"new"` — the tool is creating it, so undo deletes it again; `"delete"` — the
    file is going away, so undo puts the stored bytes back. `create` and `remove`
    are read as `new` and `delete`; anything else is refused.

    Returns the recorded entry: sequence, timestamp, tool, the path as given, the
    action, the stored blob's name, its size and the resolved target.

    Raises `JournalError` when the previous bytes could not be stored. The caller
    must then refuse to write — it has no way back any more.
    """
    action = _normalise_action(action)
    name = _name_of(workdir, path)
    target = _target_of(workdir, path)
    if action == "new" and os.path.isfile(target):
        # The caller believes it is creating the file and something is there.
        # Snapshotting is the safe answer: a `"new"` entry would have undo delete
        # bytes nobody in this session wrote.
        action = "modify"
    with _folder_lock(workdir):
        root = _usable_undo_root(workdir, create=True)
        stored = _read_manifest(root)
        seq = _next_seq(stored)
        blob, size = "", 0
        if action in ("modify", "delete"):
            data = _snapshot_of(target, action)
            size = len(data)
            blob = f"{BLOB_PREFIX}{seq:06d}-{os.getpid()}{BLOB_SUFFIX}"
            _write_blob(root / blob, data)        # the blob first ...
        entry = {"seq": seq, "ts": time.time(), "tool": str(tool), "path": str(path),
                 "target": target, "action": action, "blob": blob, "size": size}
        _append_line(root / MANIFEST_NAME, entry)  # ... then the line that names it
        _trim(root, stored + [entry])
        return dict(entry)


def _snapshot_of(target: str, action: str) -> bytes:
    """The bytes that are on disk, or the reason there are no such bytes."""
    try:
        data = Path(target).read_bytes()
    except FileNotFoundError:
        raise JournalError(L(
            f"{target} is gone, so the undo journal has nothing to store for this "
            f"`{action}` — the file changed under us; do it again once it has been "
            f"read afresh",
            f"{target} исчез, поэтому журналу нечего сохранять для `{action}` — "
            f"файл изменился у нас из-под ног; повтори, перечитав его заново"))
    except IsADirectoryError:
        raise JournalError(L(
            f"{target} is a directory, not a file, and BeeCode does not snapshot "
            f"directories",
            f"{target} — папка, а не файл; папки BeeCode не сохраняет"))
    except (OSError, UnicodeDecodeError) as e:
        raise JournalError(L(
            f"the undo journal could not read {target} ({e.__class__.__name__}: {e})",
            f"журналу не удалось прочитать {target} ({e.__class__.__name__}: {e})"))
    if len(data) > MAX_SNAPSHOT_BYTES:
        raise JournalError(L(
            f"{target} is {len(data)} bytes and the journal snapshots at most "
            f"{MAX_SNAPSHOT_BYTES} — BeeCode will not rewrite a file it cannot "
            "back up. Do it outside BeeCode if you really mean it",
            f"{target} весит {len(data)} байт, а журнал сохраняет не больше "
            f"{MAX_SNAPSHOT_BYTES}: BeeCode не будет перезаписывать файл, который "
            "не может сохранить. Делай это вне BeeCode, если оно правда нужно"))
    return data


def _write_blob(path: Path, data: bytes) -> None:
    """The snapshot through its own temporary file, so a half blob never exists."""
    tmp = Path(str(path) + ".part")
    try:
        with open(tmp, "wb") as handle:
            handle.write(data)
            handle.flush()
        os.replace(str(tmp), str(path))
    except OSError as e:
        raise JournalError(L(
            f"the undo journal could not store the previous bytes in {path} "
            f"({e.__class__.__name__}: {e}) — the disk is probably full",
            f"журнал не смог сохранить прежние байты в {path} "
            f"({e.__class__.__name__}: {e}) — похоже, диск полон"))
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def _append_line(manifest: Path, entry: dict) -> None:
    try:
        with open(manifest, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            handle.flush()
    except OSError as e:
        raise JournalError(L(
            f"the undo journal could not append to {manifest} "
            f"({e.__class__.__name__}: {e}) — the snapshot was stored, the index "
            f"was not, so nothing was recorded",
            f"журнал не смог дописать {manifest} ({e.__class__.__name__}: {e}) — "
            "снимок сохранён, строка в индексе нет, значит ничего не записано"))


def _next_seq(stored: list[dict]) -> int:
    """One past the highest sequence already in the journal."""
    high = 0
    for entry in stored:
        try:
            high = max(high, int(entry.get("seq") or 0))
        except (TypeError, ValueError):
            continue
    return high + 1


# ----------------------------------------------------------------- reading ---

def _read_manifest(root: Path) -> list[dict]:
    """Every line of the journal, oldest first, damaged ones flagged.

    A line that is not JSON, or is JSON but not an operation, stays in the list as
    a flagged placeholder. Dropping it quietly would be the same lie as a blob
    that is not there: the count the user sees has to be the count on disk.
    """
    manifest = root / MANIFEST_NAME
    if not manifest.is_file():
        return []
    try:
        with open(manifest, "r", encoding="utf-8", newline="") as handle:
            raw = handle.read()
    except (OSError, UnicodeDecodeError) as e:
        return [{"damaged": True, "seq": None, "ts": 0, "tool": "", "path": "",
                 "action": "", "target": "", "blob": "", "size": 0,
                 "why": L(f"the journal index cannot be read ({e.__class__.__name__}: {e})",
                          f"индекс журнала не читается ({e.__class__.__name__}: {e})")}]
    out: list[dict] = []
    for line in raw.splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            out.append(_damaged(line))
            continue
        if not isinstance(entry, dict) or not _looks_like_entry(entry):
            out.append(_damaged(line))
            continue
        out.append(entry)
    return out


def _looks_like_entry(entry: dict) -> bool:
    return all(name in entry for name in ("seq", "path", "action"))


def _damaged(line: str) -> dict:
    return {"damaged": True, "seq": None, "ts": 0, "tool": "", "path": "",
            "action": "", "target": "", "blob": "", "size": 0,
            "why": L("a line of the journal index is not an operation",
                     "строка индекса журнала — не операция"),
            "raw": line[:160]}


def _blob_path(root: Path, name) -> Path | None:
    """The stored copy, but only when its name is a plain file inside the journal.

    The index lives in the project folder, which a clone hands to BeeCode
    unreviewed, and `/undo` both reads that name and deletes the file it names. A
    line naming `../../../../windows/x`, or `C:\\shares\\x`, or a stream
    `notes.snap:evil`, must not turn an undo into damage somewhere else.
    """
    text = str(name or "")
    if not _BLOB_NAME.match(text):
        return None
    return root / text


def _blob_present(root: Path, entry: dict) -> bool:
    """Whether the bytes the entry names are really on disk."""
    if entry.get("action") == "new":
        return True                 # it stores nothing, so nothing can be missing
    path = _blob_path(root, entry.get("blob"))
    return bool(path) and path.is_file()


def _describe(root: Path, entry: dict) -> dict:
    """One entry as the user is shown it, with the blob checked."""
    out = dict(entry)
    out["file"] = str(out.get("path") or out.get("target") or "")
    out["shown"] = _shown(root.parent.parent, out["file"], out.get("target") or "")
    stamp = out.get("ts") or 0
    try:
        out["when"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(float(stamp)))
    except (ValueError, OSError, TypeError):
        out["when"] = "?"
    out["damaged"] = bool(out.get("damaged")) or not _blob_present(root, out)
    return out


def _shown(workdir, path: str, target: str) -> str:
    """The spelling the user typed, or the resolved name when that is all we have."""
    for candidate in (path, target):
        if not candidate:
            continue
        try:
            return str(Path(candidate).relative_to(workdir))
        except (ValueError, OSError):
            continue
    return str(path or target or "?")


def entries(workdir: str, limit: int = 20) -> list[dict]:
    """The last recorded operations, newest first — what `/undo` would roll back.

    Each carries `damaged`: the blob it points at is not on disk, so undoing it
    cannot restore anything.
    """
    root = undo_root(workdir)
    if not root.is_dir():
        return []
    wanted = max(0, int(limit))
    out: list[dict] = []
    for entry in reversed(_read_manifest(root)):
        if len(out) >= wanted:
            break
        out.append(_describe(root, entry))
    return out


# ------------------------------------------------------------------ rolling -

def undo(workdir: str, count: int = 1) -> list[dict]:
    """Roll back the last `count` recorded operations, newest first.

    Files that were modified or deleted get their stored bytes back through
    `write_text_preserving`; files the tools created get removed again. Returns
    one result per operation attempted, newest first, each the entry plus
    `undone` — `restored`, `deleted`, `gone`, `refused`, `failed` or `dropped`.
    A `refused` or `failed` entry stays in the journal, so the user can fix the
    reason (a symlink in the way, the folder it lives in) and ask again.
    """
    wanted = int(count)
    if wanted <= 0:
        return []
    with _folder_lock(workdir):
        root = undo_root(workdir)
        if not root.is_dir():
            return []
        stored = _read_manifest(root)
        if not stored:
            return []
        results: list[dict] = []
        consumed: set[int] = set()
        for position in range(len(stored) - 1, -1, -1):
            if len(results) >= wanted:
                break
            try:
                result = _rollback(root, workdir, stored[position])
            except Exception as e:            # a line nobody predicted
                result = _describe(root, stored[position])
                result["undone"] = "failed"
                result["error"] = L(
                    f"this journal line could not be rolled back "
                    f"({e.__class__.__name__}: {e}) and was left in place",
                    f"эту строку журнала не удалось отменить "
                    f"({e.__class__.__name__}: {e}); она осталась на месте")
            results.append(result)
            if result.pop("consume", False):
                consumed.add(position)
        if consumed:
            _write_manifest(root, [e for i, e in enumerate(stored) if i not in consumed])
        for position in consumed:
            _retire(root, stored[position])      # the blob is spent once undone
        return results


def _rollback(root: Path, workdir, entry: dict) -> dict:
    """One operation, backwards. Never raises: a damaged line is reported.

    Two kinds of damage are told apart on purpose. A line that is not an
    operation at all is dropped, because nothing will ever make it one and it
    would otherwise sit at the end of the journal blocking every later `/undo`.
    A line whose bytes cannot be put back *now* — a symlink in the way, a folder
    outside the roots, a read that failed — stays, so the user can fix the reason
    and ask again.
    """
    result = _describe(root, entry)
    result["consume"] = False
    action = str(entry.get("action") or "")
    if action not in ACTIONS:
        result["undone"] = "dropped"
        result["error"] = str(entry.get("why") or L(
            "this journal line is not an operation and was skipped",
            "строка журнала — не операция; она пропущена"))
        result["consume"] = True
        return result
    if action == "new":
        return _undo_new(root, workdir, entry, result)
    return _undo_bytes(root, workdir, entry, result)


def _inside_policy(entry: dict, workdir, result: dict) -> bool:
    """Containment and the symlink wall, shared by both kinds of undo.

    The policy is `guard()`'s, not a second one; the symlink rule is ours because
    a rename or an `unlink` at a linked name destroys the link rather than the
    file, which is a change to the shape of the tree that no one asked for.
    """
    target = str(entry.get("target") or "") or _target_of(workdir, entry.get("path"))
    _checked, refusal = _path_policy.guard(target, "undo")
    if refusal:
        result["undone"] = "refused"
        result["error"] = refusal
        return False
    name = _name_of(workdir, entry.get("path"))
    for candidate in {target, name}:
        if os.path.islink(candidate):
            real = os.path.realpath(candidate)
            result["undone"] = "refused"
            result["error"] = L(
                f"{candidate} is a symlink to {real}: undoing here would replace or "
                f"delete the link, not the file. Point the journal at {real} or "
                "remove the link yourself, then /undo again",
                f"{candidate} — символьная ссылка на {real}: отмена тронула бы саму "
                f"ссылку, а не файл. Убери ссылку или отмени вручную для {real}, "
                "а потом снова /undo")
            return False
    return True


def _undo_bytes(root: Path, workdir, entry: dict, result: dict) -> dict:
    """Put stored bytes back where they came from."""
    if not _inside_policy(entry, workdir, result):
        return result
    target = str(entry.get("target") or "") or _target_of(workdir, entry.get("path"))
    blob = _blob_path(root, entry.get("blob"))
    if blob is None or not blob.is_file():
        result["undone"] = "dropped"
        result["error"] = L(
            f"{target}: the saved copy the journal points at is not on disk "
            f"({entry.get('blob') or 'no blob named'}), so there is nothing to "
            "restore and the line is dropped",
            f"{target}: сохранённой копии, на которую указывает журнал, нет на диске "
            f"({entry.get('blob') or 'имя снимка не записано'}), восстанавливать "
            "нечего — строка убрана")
        result["consume"] = True
        return result
    try:
        data = blob.read_bytes()
    except OSError as e:
        result["undone"] = "failed"
        result["error"] = L(f"{target}: the saved copy could not be read ({e})",
                           f"{target}: сохранённую копию не прочитать ({e})")
        return result
    try:
        # `write_text_preserving` speaks text, and UTF-8 text that decoded
        # strictly re-encodes to the very same bytes. Anything else is a binary
        # snapshot, and BeeCode will not guess its way back over one.
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        result["undone"] = "refused"
        result["error"] = L(
            f"{target}: its saved copy is not UTF-8 text, so BeeCode will not write "
            f"it back through the text path. The bytes are intact in "
            f"{blob} — copy them over the file yourself if that is what you want",
            f"{target}: сохранённая копия — не текст UTF-8, через текстовый путь "
            f"BeeCode её не вернёт. Байты целы в {blob}: скопируй их на место сам, "
            "если это то, что нужно")
        return result
    try:
        Path(target).parent.mkdir(parents=True, exist_ok=True)
        written = write_text_preserving(target, text)
    except OSError as e:
        result["undone"] = "failed"
        result["error"] = L(
            f"{target}: the saved bytes could not be put back ({e.__class__.__name__}: {e})",
            f"{target}: прежние байты не удалось вернуть ({e.__class__.__name__}: {e})")
        return result
    result["undone"] = "restored"
    result["bytes"] = written
    result["consume"] = True
    return result


def _undo_new(root: Path, workdir, entry: dict, result: dict) -> dict:
    """Remove the file a tool created, which is what `new` promised."""
    if not _inside_policy(entry, workdir, result):
        return result
    target = str(entry.get("target") or "") or _target_of(workdir, entry.get("path"))
    if os.path.isdir(target):
        result["undone"] = "refused"
        result["error"] = L(
            f"{target} is a directory now, and undo does not delete directories",
            f"{target} теперь папка: каталоги отмена не удаляет")
        return result
    if not os.path.exists(target):
        result["undone"] = "gone"
        result["note"] = L(f"{target} is not there any more — nothing to delete",
                           f"{target} уже нет — удалять нечего")
        result["consume"] = True
        return result
    try:
        os.remove(target)
    except OSError as e:
        result["undone"] = "failed"
        result["error"] = L(f"{target} could not be removed ({e.__class__.__name__}: {e})",
                            f"{target} не удался ({e.__class__.__name__}: {e})")
        return result
    result["undone"] = "deleted"
    result["consume"] = True
    return result


def _write_manifest(root: Path, entries_list: list[dict]) -> None:
    """Rewrite the index — after a trim or an undo — through its own temp file."""
    manifest = root / MANIFEST_NAME
    tmp = Path(str(manifest) + ".part")
    body = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in entries_list)
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
            handle.flush()
        os.replace(str(tmp), str(manifest))
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise JournalError(L(
            f"the undo journal could not rewrite {manifest} ({e.__class__.__name__}: {e})",
            f"журналу не удалось перезаписать {manifest} ({e.__class__.__name__}: {e})"))


def _retire(root: Path, entry: dict) -> None:
    """Delete a blob the journal no longer points at — inside the journal only.

    The name comes out of the index, so it goes through `_blob_path`: a cloned
    project must not be able to hand `/undo clear` a list of files elsewhere to
    delete.
    """
    path = _blob_path(root, entry.get("blob"))
    if path is None:
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass        # an orphan copy is untidy; a wrong delete is not recoverable


# ------------------------------------------------------------------- caps ---

def _trim(root: Path, stored: list[dict]) -> list[dict]:
    """Keep the last MAX_ENTRIES within MAX_TOTAL_BYTES, and delete what falls out.

    The newest entry always stays, even when that single snapshot is bigger than
    the whole byte budget: the cap exists so the journal cannot grow without
    bound over time, not so that it throws away the one copy the user is about to
    ask for.
    """
    kept = list(stored)
    total = sum(max(0, int(e.get("size") or 0)) for e in kept)
    retired: list[dict] = []
    while len(kept) > MAX_ENTRIES or total > MAX_TOTAL_BYTES:
        if len(kept) <= 1:
            break
        victim = kept.pop(0)
        retired.append(victim)
        total -= max(0, int(victim.get("size") or 0))
    if retired:
        for victim in retired:
            _retire(root, victim)
        _write_manifest(root, kept)
    return kept


def clear(workdir) -> int:
    """Empty the journal: every recorded operation and every stored copy.

    Returns how many operations were dropped. Orphan blobs — a copy written by a
    record that died before its index line — go too, since nothing points at them.
    """
    with _folder_lock(workdir):
        root = undo_root(workdir)
        if not root.is_dir():
            return 0
        stored = _read_manifest(root)
        named = {str(e.get("blob") or "") for e in stored}
        for entry in stored:
            _retire(root, entry)
        for stray in list(root.iterdir()):
            if stray.is_file() and (stray.name.startswith(BLOB_PREFIX)
                                    or stray.name.endswith(".part")) \
                    and stray.name not in named:
                try:
                    stray.unlink()
                except OSError:
                    pass
        try:
            (root / MANIFEST_NAME).unlink(missing_ok=True)
        except OSError as e:
            raise JournalError(L(
                f"the undo journal could not be emptied ({e.__class__.__name__}: {e})",
                f"журнал отмены не очистился ({e.__class__.__name__}: {e})"))
        return len(stored)


def recoverable_clause(entry: dict) -> str:
    """The short clause a tool result ends with, so the transcript says the
    safety net caught this change.

    Every file tool needs the same words, and a tool result that promises a
    recoverable file without naming the operation the journal holds is the kind
    of claim this module exists to make true.
    """
    seq = entry.get("seq")
    if str(entry.get("action") or "") == "new":
        return L(f" — new file, /undo #{seq} removes it again",
                 f" — новый файл, /undo #{seq} его уберёт")
    return L(f" — previous bytes saved, /undo #{seq} puts them back",
             f" — прежние байты сохранены, /undo #{seq} их вернёт")


def refusal(tool: str, error) -> str:
    """What a file tool answers when the journal could not record the change.

    The wording has to be a refusal and not a failure note: nothing here may read
    like a result, because a message that looks like success is the worst bug this
    project has. It deliberately avoids the words the tools use when they do write.
    """
    reason = str(error)
    return L(f"`{tool}` refused: the undo journal could not store the bytes that "
             f"are about to be replaced, and BeeCode does not change a file it "
             f"cannot put back — {reason}",
             f"`{tool}` отказал: журнал отмены не смог сохранить байты, которые "
             f"вот-вот заменятся, а BeeCode не меняет файл, который не может "
             f"вернуть — {reason}")


# --------------------------------------------------------- the one command --

COMMAND_NAME = "undo"
USAGE = "/undo [n|list|clear]"
DESCRIPTION = "Undo what BeeCode wrote to your files, newest first"


def register_command() -> bool:
    """Put `/undo` into the shared command machinery.

    The same trick `core/trust.py` plays: both interfaces go through
    `commands.dispatch`, so registering into `COMMANDS`/`HANDLERS` here covers
    the classic REPL, the Textual sidebar and one-shot runs without editing
    commands.py. Idempotent, and the category is a core one so no plugin can
    take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_undo:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="engine")
    core.HANDLERS[COMMAND_NAME] = _cmd_undo
    return True


def _cmd_undo(ctx, args):
    """`/undo` lists, `/undo <n>` rolls back n, `/undo clear` empties the journal."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    workdir = getattr(getattr(ctx, "agent", None), "workdir", None) or "."
    word = (args[0].strip().lower() if args else "")

    if word in ("", "list", "-l", "--list"):
        return CommandResult(output=_listing(workdir))
    if word in ("clear", "reset", "empty"):
        try:
            dropped = clear(workdir)
        except JournalError as e:
            return CommandResult(output=Text(str(e), style="bold yellow"))
        return CommandResult(output=Text(L(
            f"the undo journal is empty — {dropped} operation(s) and their stored "
            f"bytes dropped",
            f"журнал отмены пуст: убрано операций — {dropped}, вместе с их снимками"),
            style="bold"))
    digits = word.lstrip("+")
    if not (digits.isascii() and digits.isdigit()):
        return CommandResult(output=Text(f"usage: {USAGE}", style="bold"))
    how_many = max(1, min(int(digits), MAX_ENTRIES))
    try:
        results = undo(workdir, how_many)
    except JournalError as e:
        return CommandResult(output=Text(str(e), style="bold yellow"))
    return CommandResult(output=Text("\n".join(_undo_lines(workdir, results, how_many))))


def _undo_lines(workdir, results: list[dict], how_many: int) -> list[str]:
    """What undo says, one line per operation it attempted."""
    if not results:
        return [L("nothing to undo — BeeCode has not recorded a change in "
                  f"{workdir}",
                  f"отменять нечего — BeeCode не записывал изменений в {workdir}")]
    head = L(f"undo: {len(results)} of {how_many} asked rolled back",
             f"отмена: вернуто {len(results)} из {how_many}")
    lines = [head]
    for result in results:
        state = str(result.get("undone"))
        shown = result.get("shown") or result.get("file") or "?"
        detail = str(result.get("error") or result.get("note") or "")
        label = {
            "restored": L("restored", "вернул"),
            "deleted": L("deleted", "удалил"),
            "gone": L("already gone", "уже не было"),
            "dropped": L("dropped", "убрано"),
            "refused": L("REFUSED", "ОТКАЗ"),
            "failed": L("FAILED", "СБОЙ"),
        }.get(state, state)
        line = f"  #{result.get('seq')}: {label} {shown} " \
               f"({result.get('tool')} {result.get('action')})"
        lines.append(f"{line} — {detail}" if detail else line)
    lines.append(L("the journal shows what is left: /undo", "что осталось: /undo"))
    return lines


def _listing(workdir):
    """The listing `/undo` answers with: what is recorded, and what undoing means.

    A group of table plus the lines under it, because the table on its own does
    not say what typing `/undo 3` would do to the files the user cares about.
    """
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    listed = entries(workdir, PREVIEW_LIMIT)
    frame = {}
    try:
        from beeagent.ui import skin

        frame = skin.frame_kwargs("#ffcc00")
    except Exception:                   # a listing must not fail over a colour
        frame = {}
    table = Table(title=L("🐝 undo journal", "🐝 журнал отмены"), show_header=True,
                  header_style="bold #ffcc00", **frame)
    table.add_column("#", style="dim")
    table.add_column(L("when", "когда"), style="dim")
    table.add_column(L("tool", "инструмент"), style="dim")
    table.add_column(L("file", "файл"))
    table.add_column(L("what undo does", "что сделает отмена"))
    for entry in listed:
        table.add_row(str(entry.get("seq") or "?"), str(entry.get("when") or "?"),
                      str(entry.get("tool") or "?"),
                      str(entry.get("shown") or entry.get("file") or "?"),
                      _means(entry))
    note = Text("\n".join(_listing_note(workdir, listed)))
    if table.row_count:
        return Group(table, note)
    return note


def _means(entry: dict) -> str:
    """What rolling this one operation back does to the file."""
    action = str(entry.get("action") or "")
    if entry.get("damaged"):
        return L("cannot: the saved copy is gone", "не сможет: снимка нет")
    if action == "new":
        return L("delete the file it created", "удалит созданный файл")
    if action == "delete":
        return L("put the deleted file back", "вернёт удалённый файл")
    if action == "modify":
        return L("restore the bytes it replaced", "вернёт прежние байты")
    return L("damaged line, skipped", "повреждённая строка, пропуск")


def _listing_note(workdir, listed: list[dict]) -> list[str]:
    """The words under the table: what to type, and what is not shown."""
    total = len(entries(workdir, MAX_ENTRIES))
    lines = []
    if not listed:
        lines.append(L(f"nothing is recorded in {workdir} yet: BeeCode has not "
                       "written a file since the journal opened",
                       f"в {workdir} пока ничего не записано: BeeCode не писал файлов, "
                       "с тех пор как открыт журнал"))
        return lines
    lines.append(L("that is what /undo rolls back, newest first",
                   "это то, что отменит /undo, начиная с последнего"))
    lines.append(L("type /undo to undo the last change, /undo <n> for the last n, "
                   "/undo clear to empty the journal",
                   "напиши /undo — вернёт последнее, /undo <n> — последние n, "
                   "/undo clear — очистить журнал"))
    if total > len(listed):
        lines.append(L(f"{total - len(listed)} older operation(s) are recorded "
                       f"beyond these {len(listed)}",
                       f"ещё {total - len(listed)} прежних записей сверх этих "
                       f"{len(listed)}"))
    return lines
