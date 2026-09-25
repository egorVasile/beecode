"""`patch` — one unified diff, several files, one decision.

Refactoring five files with `edit` costs five calls and five exact strings, and
the fourth usually fails after the first three already landed. A diff is one
artifact the model can produce, review and re-send whole, so this tool takes the
patch text and treats it as a single transaction: every hunk of every file is
located and verified in memory, and the disk sees bytes only when the whole diff
survives that pass. Anything else writes nothing at all and says which file,
which hunk and which line disagreed — "applied" meaning "applied 3 of 5" is the
lie this project counts as its worst bug class.

Three rules hold it together:

* **verify, never guess.** A hunk is checked against the file's real lines. The
  declared line number is a hint, not a fact: when the block matches exactly one
  other place the hunk goes there and the offset is announced; when it matches
  several places the hunk is refused. Fuzzy matching is never used — a near miss
  has to reach the model's eyes, because a near miss by a patch tool is a wrong
  program that looks applied.
* **one write per file, through the house helpers.** The new content is built in
  full and handed to `write_text_preserving`, which writes a side file and
  renames, so a killed process cannot leave half a file behind. Line endings are
  compared normalized and written as the file's own, exactly like `edit` does.
* **no undo journal, no patch.** `patch` overwrites whole files; without
  `beeagent/core/journal.py` the bytes it drops are gone for good, so the call
  refuses rather than doing unrecoverable work behind the user's back.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from beeagent.i18n import L

from ._path_policy import guard
from ._seen import changed_since_read, forget, remember
from .base import BaseTool, ToolResult, read_text_preserving, write_text_preserving

NULL = "/dev/null"

# A hunk header with the counts git writes, the counts plain `diff -u` writes
# without a comma (one line), and the section heading some tools append.
HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:[ \t].*)?$")
FILE_LINE_RE = re.compile(r"^(---|\+\+\+)[ \t]+(.*)$")
GIT_HEADER_RE = re.compile(r"^diff --git\b")
MODE_CHANGE_RE = re.compile(r"^(?:old|new) mode (\d{3,7})$")
NEW_FILE_MODE_RE = re.compile(r"^new file mode (\d{3,7})$")
DELETED_FILE_MODE_RE = re.compile(r"^deleted file mode (\d{3,7})$")
RENAME_RE = re.compile(r"^(?:rename|copy) (?:from|to) ")
SIMILARITY_RE = re.compile(r"^similarity index \d+%$")
BINARY_RE = re.compile(r"^(?:GIT binary patch\b|Binary files\b)")
INDEX_RE = re.compile(r"^index [0-9A-Za-z]+(?:\.\.[0-9A-Za-z]+)?(?: (\d{3,7}))?$")
OTHER_FORMAT_RE = re.compile(r"^(?:Index: |===+|\*\*\* )")
NO_NEWLINE = "No newline at end of file"

# Header lines that belong to a file section rather than to a hunk body.
META_RE = re.compile(
    r"^(?:old mode |new mode |new file mode |deleted file mode |rename (?:from|to) |"
    r"copy (?:from|to) |similarity index |index |Binary files |GIT binary patch|\\ )")


@dataclass
class _Hunk:
    """One `@@` block: what it claims to remove and what it claims to leave."""

    header: str
    old_start: int
    old_count: int
    new_count: int
    before: list[str] = field(default_factory=list)   # context + removed, endings stripped
    after: list[str] = field(default_factory=list)    # context + added, endings stripped
    old_no_final_newline: bool = False
    new_no_final_newline: bool = False

    def name(self) -> str:
        return f"`{self.header}`"


@dataclass
class _Section:
    """One file as the diff describes it, before anything touches the disk."""

    old: str | None = None
    new: str | None = None
    hunks: list[_Hunk] = field(default_factory=list)


@dataclass
class _Job:
    """A file that passed every check and only waits for the write phase."""

    label: str
    target: Path
    action: str            # "new" | "modify" | "delete"
    content: str
    notes: list[str]
    shifts: list[str]
    stamp: tuple
    old_bytes: int
    written: int = 0


class PatchTool(BaseTool):
    name = "patch"
    description = (
        "Apply one unified diff across several files in a single call: `--- a/path` and "
        "`+++ b/path`, then `@@ -l,s +l,s @@` and lines starting with a space, `-` or `+`. "
        "`--- /dev/null` creates, `+++ /dev/null` deletes. Copy the context from the file "
        "as it is now: every hunk is checked against the bytes on disk, a hunk that only "
        "moved is applied where it matches and the offset is reported, and if any hunk is "
        "refused NO file is written, so re-sending the whole diff after a fix is safe. "
        "Renames, binary content and paths outside the working directory are refused. "
        "Needs the user's /allow patch."
    )
    parameters = {
        "type": "object",
        "properties": {
            "diff": {
                "type": "string",
                "description": ("The unified diff body, as one JSON string: every line break "
                                "written as \\n and every backslash as \\\\."),
            },
            "root": {
                "type": "string",
                "description": ("Directory the paths in the diff are relative to; defaults to the "
                                "working directory and must itself be inside it."),
            },
        },
        "required": ["diff"],
    }
    writes_files = True

    def execute(self, diff: str = "", root: str = "") -> ToolResult:
        try:
            return self._run(diff, root)
        except Exception as e:                       # noqa: BLE001 - one honest error line
            return ToolResult(output=L(f"`patch` failed: {e}", f"`patch` не выполнился: {e}"),
                              error=True)

    def is_safe(self) -> bool:
        return False

    # ------------------------------------------------------------------- run ---

    def _run(self, diff, root) -> ToolResult:
        if not isinstance(diff, str) or not diff.strip():
            return ToolResult(
                output=L("`patch` needs the `diff` parameter: the whole unified diff as one "
                         "string, with newlines written as \\n",
                         "`patch` нужен параметр `diff`: весь unified diff одной строкой, где "
                         "переносы записаны как \\n"), error=True)
        base = root if isinstance(root, str) and root.strip() else "."
        root_dir, refusal = guard(base, "patch")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})
        if not root_dir.is_dir():
            return ToolResult(
                output=L(f"`root` is not a directory: {root}", f"`root` не каталог: {root}"),
                error=True)

        sections, problems = _parse(diff)
        if problems:
            return _refused(L("the diff itself cannot be applied as written",
                              "этот diff нельзя применить так, как он написан"), problems, [])
        jobs, failures = _plan(sections, root_dir)
        if failures:
            return _refused(L("nothing was written", "ничего не записано"), failures,
                            [job.label for job in jobs])

        blocked = _journal_gate(jobs, root_dir)
        if blocked:
            return blocked
        return _commit(jobs)


# --------------------------------------------------------------- the journal ---

def journal_record():
    """`beeagent.core.journal.record`, or None when there is no undo journal.

    The import is guarded because the journal may not be installed, may not
    import (a broken optional dependency inside it) or may not exist yet. All
    three answer the same way, and it is not "go ahead": nothing here may
    overwrite bytes that then become impossible to give back.
    """
    try:
        from beeagent.core import journal
    except Exception:                                # noqa: BLE001 - absence is the answer
        return None
    record = getattr(journal, "record", None)
    return record if callable(record) else None


def _journal_gate(jobs: list[_Job], root_dir: Path) -> ToolResult | None:
    """Log every target before the first byte is written; refuse on any doubt."""
    record = journal_record()
    if record is None:
        return ToolResult(
            output=L("`patch` refused: BeeCode has no undo journal "
                     "(`beeagent/core/journal.py` is missing or `record()` did not load), so the "
                     "bytes this patch overwrites could not be given back. Nothing was written. "
                     "Repair the installation (or update BeeCode) and send the same diff again, "
                     "or change one file at a time with `edit`.",
                     "`patch` отказал: у BeeCode нет журнала отмены "
                     "(`beeagent/core/journal.py` отсутствует или `record()` не загрузился), "
                     "поэтому байты, которые этот патч перезапишет, не вернуть. Ничего не "
                     "записано. Почини установку (или обнови BeeCode) и пришли тот же diff "
                     "заново — либо меняй по одному файлу через `edit`."),
            error=True, metadata={"refused": "no-journal"})
    names = ", ".join(job.label for job in jobs)
    try:
        for job in jobs:
            outcome = record(str(root_dir), "patch", str(job.target), action=job.action)
            if outcome is False:
                # A journal that says "not stored" has to be believed: it is the
                # only component that knows whether the previous bytes survived.
                raise RuntimeError("journal.record() reported that it stored nothing")
    except Exception as e:                           # noqa: BLE001 - a refused patch either way
        return ToolResult(
            output=L(f"`patch` refused: the undo journal did not accept this operation ({e}), so "
                     f"the current bytes of {names} would be unrecoverable. Nothing was written; "
                     f"no file in this diff changed.",
                     f"`patch` отказал: журнал отмены не принял эту операцию ({e}), поэтому "
                     f"текущие байты {names} были бы невосстановимы. Ничего не записано; ни один "
                     f"файл этого diff не изменён."),
            error=True, metadata={"refused": "journal"})
    return None


# ------------------------------------------------------------------ parsing ---

def _parse(text: str) -> tuple[list[_Section], list[str]]:
    """Split the patch into sections and hunks, or say why it is not a patch.

    Only LF counts as a line break here: `str.splitlines()` also breaks on a form
    feed, and a `^L` inside a source file would cut a context line in half.
    """
    if text.lstrip().startswith("```"):
        # Models paste fences around the patch. A fence cannot begin a real diff
        # line, so drop it and its partner; the text between still has to parse.
        head, _, tail = text.lstrip().partition("\n")
        if head.strip().lower().startswith("```diff") or head.strip() == "```":
            text = tail.rsplit("```", 1)[0] if "```" in tail else tail
    lines = text.split("\n")
    # A CR ending a diff line is that line's own terminator (the patch was written
    # on Windows), never part of its content.
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    while lines and lines[-1] == "":
        lines.pop()                      # the newline that ended the argument, not a line

    sections: list[_Section] = []
    current: _Section | None = None
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if line.startswith("@@"):
            if current is None or current.old is None or current.new is None:
                return [], [L(f"the hunk {line[:60]!r} has no `--- `/`+++ ` file header above it",
                              f"у хатки {line[:60]!r} нет заголовка файла `--- `/`+++ `")]
            hunk, i, error = _read_hunk(lines, i)
            if error:
                return [], [error]
            current.hunks.append(hunk)
            continue
        match = FILE_LINE_RE.match(line)
        if match:
            side, raw = match.group(1), _clean_path(match.group(2))
            if side == "---":
                if current is not None and current.old is not None and current.new is None:
                    return [], [L(f"`{line[:60]}` follows a file header that never got its "
                                  f"`+++ b/...` line",
                                  f"`{line[:60]}` идёт за заголовком файла, в котором так и не "
                                  f"появилось `+++ b/...`")]
                current = _Section(old=raw)     # a complete section above it closes here
                sections.append(current)
            elif current is None:
                return [], [L(f"`{line[:60]}` has no `--- a/...` line above it",
                              f"перед `{line[:60]}` нет строки `--- a/...`")]
            else:
                current.new = raw
            i += 1
            continue
        if GIT_HEADER_RE.match(line) or META_RE.match(line) or INDEX_RE.match(line):
            problem = _meta_problem(line)
            if problem:
                return [], [problem]
            if GIT_HEADER_RE.match(line):
                current = None           # the paths come from ---/+++, never from this line
            i += 1
            continue
        if OTHER_FORMAT_RE.match(line):
            return [], [L(f"`patch` reads plain unified diffs: {line[:60]!r} belongs to another "
                          f"format (context diff, `diff -p`, or SVN output)",
                          f"`patch` понимает только обычный unified diff: {line[:60]!r} из "
                          f"другого формата (контекстный diff, `diff -p`, вывод SVN)")]
        return [], [L(f"`patch` cannot read this diff: unexpected line {line[:60]!r}. A hunk "
                      f"line starts with a space, +, -, \\ or @@, and every file needs its "
                      f"`--- a/path` and `+++ b/path` lines",
                      f"`patch` не может прочитать этот diff: непонятная строка {line[:60]!r}. "
                      f"Строка хатки начинается с пробела, +, -, \\ или @@, а у каждого файла "
                      f"должны быть строки `--- a/путь` и `+++ b/путь`")]

    for section in sections:
        if section.old is None or section.new is None:
            return [], [L("a file header is incomplete: both `--- ` and `+++ ` lines are "
                          "required, and `/dev/null` stands for the side that has no file",
                          "заголовок файла неполон: нужны обе строки, `--- ` и `+++ `, а "
                          "сторона без файла записывается как `/dev/null`")]
    return sections, []


def _meta_problem(line: str) -> str:
    """A header that promises something a text patch cannot deliver."""
    match = MODE_CHANGE_RE.match(line)
    if match:
        return L(f"`{line}` declares a file mode change. `patch` changes content only: a chmod "
                 f"is the user's own command, never a side effect of applying a diff",
                 f"`{line}` — смена режима файла. `patch` меняет только содержимое: chmod — это "
                 f"команда пользователя, а не побочный эффект наложения патча")
    match = NEW_FILE_MODE_RE.match(line)
    if match and _exec_bit(match.group(1)):
        return L(f"`{line}` creates an executable file, and `patch` cannot set the executable "
                 f"bit. Create it with `write`, then ask the user to run chmod",
                 f"`{line}` создаёт исполняемый файл, а `patch` не ставит бит исполнения. "
                 f"Создай его через `write`, а chmod пусть выполнит пользователь")
    if RENAME_RE.match(line) or SIMILARITY_RE.match(line):
        return L(f"`{line}` is a rename or a copy. `patch` does not move files: write the new "
                 f"path and delete the old one as its own diff, so the user sees both steps",
                 f"`{line}` — переименование или копия. `patch` файлы не переносит: создай новый "
                 f"путь через `write`, а старый удали отдельным diff, чтобы пользователь видел "
                 f"оба шага")
    if BINARY_RE.match(line):
        return L(f"`{line}` is a binary patch. `patch` is a text tool and will not guess at "
                 f"base85 or delta data: that file has to be replaced by whatever produced it",
                 f"`{line}` — бинарный патч. `patch` работает с текстом и не будет гадать по "
                 f"base85 или delta: файл должен заменить тот инструмент, который его создал")
    match = INDEX_RE.match(line)
    if match and match.group(1) == "160000":
        return L(f"`{line}` names a git submodule (mode 160000). Registering or unregistering a "
                 f"submodule is not a content edit and is refused",
                 f"`{line}` — подмодуль git (режим 160000). Подключать или отключать подмодуль "
                 f"неправильно называть правкой содержимого, отказано")
    return ""


def _exec_bit(mode: str) -> bool:
    try:
        return bool(int(mode, 8) & 0o111)
    except ValueError:
        return True                       # an unreadable mode is treated as "not a plain file"


def _clean_path(raw: str) -> str:
    """The path a header line means, without git's timestamp or its C-quoting."""
    text = raw.split("\t")[0].strip()
    if len(text) >= 2 and text.startswith('"') and text.endswith('"'):
        text = _unescape(text[1:-1])
    return text


def _unescape(text: str) -> str:
    """Undo git's `"a\\tb.py"`-style quoting, the only place an escape is meant literally."""
    simple = {"n": "\n", "r": "\r", "t": "\t", '"': '"', "\\": "\\"}
    out, i = [], 0
    while i < len(text):
        char = text[i]
        if char == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            if nxt in simple:
                out.append(simple[nxt])
                i += 2
                continue
            if nxt.isdigit() and i + 4 < len(text):
                try:
                    out.append(chr(int(text[i + 2:i + 5], 8)))
                    i += 5
                    continue
                except ValueError:
                    pass
        out.append(char)
        i += 1
    return "".join(out)


def _read_hunk(lines: list[str], start: int) -> tuple[_Hunk | None, int, str]:
    """Consume one hunk by its declared counts.

    The counts decide where the body ends, not the shape of the next line: a
    removed line whose text is `-- foo` prints as `--- foo`, which is otherwise
    indistinguishable from a file header. The counts are then *verified* against
    what arrived, because a context line counts once on each side and a model
    that miscounts is describing a hunk that cannot be applied as written.
    """
    header = lines[start]
    match = HUNK_RE.match(header)
    if not match:
        return None, start, L(f"unreadable hunk header {header[:60]!r}; the form is "
                              f"`@@ -old_start,old_count +new_start,new_count @@`",
                              f"нечитаемая хатка {header[:60]!r}; форма такая: "
                              f"`@@ -начало,количество +начало,количество @@`")
    old_count = int(match.group(2)) if match.group(2) is not None else 1
    new_count = int(match.group(4)) if match.group(4) is not None else 1
    body: list[tuple[str, str]] = []
    kept = dropped = added = 0        # context, '-', '+' lines
    old_no_newline = new_no_newline = False
    i, n = start + 1, len(lines)
    while i < n:
        text = lines[i]
        if text.startswith("\\"):
            if not text[1:].strip().startswith(NO_NEWLINE):
                return None, i, L(f"unknown marker line {text[:60]!r} inside a hunk",
                                  f"неизвестная строка-маркер {text[:60]!r} внутри хатки")
            # The marker speaks about the line above it, on whichever side that
            # line exists: `-` old only, `+` new only, ` ` both.
            if body and body[-1][0] in "- ":
                old_no_newline = True
            if body and body[-1][0] in "+ ":
                new_no_newline = True
            i += 1
            continue
        if kept + dropped >= old_count and kept + added >= new_count:
            break                          # the hunk is complete; the header owns the rest
        kind = text[0] if text else " "     # a blank context line with its space eaten
        if kind not in " +-":
            break
        if kind == "-" and kept + dropped >= old_count:
            break                         # the old side is full: this is a header, not a `-`
        if kind == "+" and kept + added >= new_count:
            break                         # the new side is full: this is a header, not a `+`
        body.append((kind, text[1:]))
        if kind == " ":
            kept += 1
        elif kind == "-":
            dropped += 1
        else:
            added += 1
        i += 1
    if kept + dropped != old_count or kept + added != new_count:
        return None, i, L(
            f"hunk {header[:40]!r} declares {old_count} old and {new_count} new lines, but its "
            f"body carries {kept} context, {dropped} removed and {added} added "
            f"({kept + dropped} old, {kept + added} new). Fix the numbers after `@@` or the "
            f"body: a hunk whose counts lie is refused",
            f"хатка {header[:40]!r} обещает {old_count} старых и {new_count} новых строк, а в "
            f"теле {kept} контекста, {dropped} удалённых и {added} добавленных "
            f"(старых {kept + dropped}, новых {kept + added}). Исправь числа после `@@` либо "
            f"тело: хатка с неверными счётчиками отклоняется")
    return (_Hunk(header=header, old_start=int(match.group(1)), old_count=old_count,
                  new_count=new_count,
                  before=[t for k, t in body if k in " -"],
                  after=[t for k, t in body if k in " +"],
                  old_no_final_newline=old_no_newline, new_no_final_newline=new_no_newline),
            i, "")


# ------------------------------------------------------- plan (memory only) ---

def _plan(sections: list[_Section], root_dir: Path) -> tuple[list[_Job], list[str]]:
    """Build every file's new content in memory. Nothing here writes."""
    jobs: list[_Job] = []
    failures: list[str] = []
    for section in sections:
        label, reasons, job = _plan_one(section, root_dir)
        if reasons:
            failures.extend(f"{label}: {reason}" for reason in reasons)
        else:
            jobs.append(job)
    return jobs, failures


def _plan_one(section: _Section, root_dir: Path):
    """(label, reasons, job) for one file section."""
    old, new = _strip_pair(section.old, section.new)
    created, deleted = old == NULL, new == NULL
    if created and deleted:
        return (new or old or "<no path>"), [L("both header lines say /dev/null, so this diff "
                                               "neither creates nor deletes anything",
                                               "обе строки — /dev/null, то есть этот diff ничего "
                                               "не создаёт и не удаляет")], None
    label = old if deleted else new
    if not created and not deleted and old != new:
        return label, [L(f"the header names two different files (`{old}` -> `{new}`), which is a "
                         f"rename; `patch` does not move content from one path to another",
                         f"заголовок называет два разных файла (`{old}` -> `{new}`) — это "
                         f"переименование; `patch` не переносит содержимое между путями")], None
    if not label.strip():
        return "<no path>", [L("the file header names no path at all",
                               "заголовок файла не называет путь")], None
    escaped = _home_in_path(label)
    if escaped:
        return label, [L(f"`{label}` puts a `{escaped}` component in the path. In a diff that "
                         f"spelling means somebody's home directory, not a folder inside the "
                         f"project, and `patch` will not create either",
                         f"`{label}` содержит компонент `{escaped}` в пути. В diff такое "
                         f"написание означает домашний каталог, а не папку внутри проекта, и "
                         f"`patch` не создаст ни то, ни другое")], None

    raw = label if os.path.isabs(label) else str(root_dir / label)
    target, refusal = guard(raw, "patch")
    if refusal:
        return label, [refusal], None
    if not _within(target, root_dir):
        return label, [L(f"{label!r} resolves to `{target}`, which is outside the `root` you "
                         f"gave (`{root_dir}`): every path in one diff has to live under that "
                         f"root",
                         f"{label!r} — это `{target}`, вне указанного `root` (`{root_dir}`): все "
                         f"пути одного diff должны лежать внутри этого корня")], None
    if target.is_dir():
        return label, [L(f"{label} is a directory, not a file",
                         f"{label} — каталог, а не файл")], None

    if created and target.exists():
        return label, [L(f"the diff creates {label}, but the file already exists "
                         f"({target.stat().st_size} bytes). Apply hunks against its real "
                         f"content, or replace it deliberately with `write`",
                         f"diff создаёт {label}, но файл уже существует "
                         f"({target.stat().st_size} байт). Наложись хатками на его реальное "
                         f"содержимое или замени его осознанно через `write`")], None
    if not created and not target.exists():
        return label, [L(f"{label} is not there, and this diff does not declare it as created "
                         f"(`--- /dev/null`). List the folder and resend the diff against the "
                         f"files that exist",
                         f"{label} не существует, а diff не объявляет его создание "
                         f"(`--- /dev/null`). Посмотри содержимое папки и пришли diff по тем "
                         f"файлам, которые есть")], None
    if changed_since_read(target):
        return label, [L(f"{label} changed since you read it, so this diff was written against "
                         f"bytes that no longer exist. Read it again and resend the whole diff",
                         f"{label} изменился с тех пор, как ты его читал: diff написан по байтам, "
                         f"которых уже нет. Прочитай заново и пришли весь diff")], None

    stamp = _stamp(target)
    content = ""
    if not created:
        try:
            content = read_text_preserving(target)
        except UnicodeDecodeError as e:
            return label, [L(f"{label} is not UTF-8 text ({e.reason} at byte {e.start}), so a "
                             f"text patch cannot describe it. Its bytes stay as they are",
                             f"{label} — не текст в UTF-8 ({e.reason} на байте {e.start}); "
                             f"текстовый патч к нему неприменим. Его байты остались как были")], None
        except OSError as e:
            return label, [L(f"{label} could not be read ({e})",
                             f"{label} не удалось прочитать ({e})")], None
    old_bytes = len(content.encode("utf-8"))

    if not section.hunks:
        if created:
            # git writes no hunk at all for an empty new file, and that is the
            # one "changes nothing" section with a real effect to carry out.
            return label, [], _Job(label=label, target=target, action="new", content="",
                                   notes=[L("created empty (the diff declares no hunk)",
                                            "создан пустым (diff без хаток)")],
                                   shifts=[], stamp=stamp, old_bytes=0)
        if deleted:
            return label, [L(f"{label}: deleting a file without the hunks that remove its lines "
                             f"is refused; show what is being deleted",
                             f"{label}: удалять файл без хаток, снимающих его строки, нельзя; "
                             f"покажи, что именно удаляется")], None
        return label, [L(f"{label}: this header has no hunk, so the diff changes nothing about "
                         f"it. A section that does no work is refused instead of being counted "
                         f"as applied",
                         f"{label}: у этого заголовка нет хатки, то есть diff в нём ничего не "
                         f"меняет. Раздел без работы отказывает, а не числится применённым")], None

    new_content, notes, shifts, reasons = _apply(label, content, section.hunks)
    if not reasons and deleted and new_content:
        reasons.append(L(f"the diff deletes {label}, but {_count_lines(new_content)} line(s) of "
                         f"it survive the hunks: a deletion has to take the whole file",
                         f"diff удаляет {label}, но после хаток от него остаётся "
                         f"{_count_lines(new_content)} строк(и): удаление обязано снять весь "
                         f"файл"))
    if reasons:
        return label, reasons, None
    action = "new" if created else ("delete" if deleted else "modify")
    return label, [], _Job(label=label, target=target, action=action,
                           content="" if deleted else new_content, notes=notes, shifts=shifts,
                           stamp=stamp, old_bytes=old_bytes)


def _home_in_path(label: str) -> str:
    """A `~` or `~user` component, which `guard` would only see once it is joined in."""
    for part in re.split(r"[\\/]", label):
        if part.startswith("~"):
            return part
    return ""


def _strip_pair(old: str, new: str) -> tuple[str, str]:
    """Drop git's `a/`+`b/` prefixes, and `./` from hand-written diffs."""
    if old and new:
        if old.startswith("a/") and new.startswith("b/"):
            return old[2:], new[2:]
        if old.startswith("./") and new.startswith("./"):
            return old[2:], new[2:]
    if old == NULL and new.startswith(("b/", "./")):
        return old, new[2:]
    if new == NULL and old.startswith(("a/", "./")):
        return old[2:], new
    return old, new


def _within(child: Path, parent: Path) -> bool:
    left = os.path.normcase(str(child)).rstrip("\\/")
    right = os.path.normcase(str(parent)).rstrip("\\/")
    return left == right or left.startswith(right + os.sep)


def _count_lines(text: str) -> int:
    return len(_split_lines(text))


def _apply(label: str, content: str, hunks: list[_Hunk]):
    """Rebuild the file: (new content, notes, shifts, reasons).

    The untouched lines are carried verbatim, so every byte outside a hunk —
    including the file's own line endings — survives. Only lines a hunk writes
    get an ending, and that ending is the file's prevailing one, which is how a
    LF-only diff still lands cleanly on a CRLF file.
    """
    bom = content.startswith("\ufeff")
    body = content[1:] if bom else content
    raw_lines = _split_lines(body)
    norm = [_text_of(line) for line in raw_lines]
    ending = _prevailing(raw_lines)
    out: list[str] = []
    notes: list[str] = []
    shifts: list[str] = []
    reasons: list[str] = []
    cursor = 0
    for index, hunk in enumerate(hunks):
        position, offset, reason = _locate(label, hunk, norm)
        if reason:
            reasons.append(reason)
            continue
        if position < cursor:
            reasons.append(L(f"hunk {hunk.name()} lands at line {position + 1} while the "
                             f"previous hunk already consumed through line {cursor}: hunks "
                             f"overlap or are out of order",
                             f"хатка {hunk.name()} попадает на строку {position + 1}, тогда как "
                             f"предыдущая дошла до строки {cursor}: хатки перекрываются или идут "
                             f"не по порядку"))
            continue
        if hunk.old_no_final_newline and not _last_line_is_unterminated(raw_lines, position,
                                                                        hunk):
            reasons.append(L(f"hunk {hunk.name()} carries `\\ {NO_NEWLINE}` for the old side, "
                             f"but that line of {label} does end with a newline",
                             f"хатка {hunk.name()} несёт `\\ {NO_NEWLINE}` для старого файла, но "
                             f"эта строка {label} заканчивается переносом"))
            continue
        if hunk.new_no_final_newline and (index != len(hunks) - 1
                                          or position + hunk.old_count != len(raw_lines)):
            reasons.append(L(f"hunk {hunk.name()} carries `\\ {NO_NEWLINE}` for the new side, but "
                             f"it is not the end of {label}",
                             f"хатка {hunk.name()} несёт `\\ {NO_NEWLINE}` для нового файла, но "
                             f"это не конец {label}"))
            continue
        out.extend(raw_lines[cursor:position])
        for step, text in enumerate(hunk.after):
            last = step == len(hunk.after) - 1
            out.append(text if last and hunk.new_no_final_newline else text + ending)
        notes.append(_note(index + 1, hunk, position, offset))
        if offset:
            shifts.append(_shift(index + 1, hunk, position, offset))
        cursor = position + hunk.old_count
    out.extend(raw_lines[cursor:])
    if reasons:
        return "", notes, shifts, reasons
    joined = "".join(out)
    return ("\ufeff" + joined if bom else joined), notes, shifts, reasons


def _last_line_is_unterminated(raw_lines: list[str], position: int, hunk: _Hunk) -> bool:
    """True when the marked old line really is the file's unterminated last line."""
    return (bool(raw_lines) and position + len(hunk.before) == len(raw_lines)
            and not raw_lines[-1].endswith(("\n", "\r")))


def _note(index: int, hunk: _Hunk, position: int, offset: int) -> str:
    if offset:
        return L(f"hunk {index} {hunk.header}: applied at line {position + 1}, "
                 f"{abs(offset)} line(s) {'later' if offset > 0 else 'earlier'} than the "
                 f"declared line {hunk.old_start}",
                 f"хатка {index} {hunk.header}: применена на строке {position + 1}, на "
                 f"{abs(offset)} строк {'позже' if offset > 0 else 'раньше'} заявленной "
                 f"{hunk.old_start}")
    return L(f"hunk {index} {hunk.header}: applied at line {position + 1}",
             f"хатка {index} {hunk.header}: применена на строке {position + 1}")


def _shift(index: int, hunk: _Hunk, position: int, offset: int) -> str:
    return L(f"hunk {index} moved {abs(offset)} line(s) "
             f"{'later' if offset > 0 else 'earlier'} (declared {hunk.old_start}, applied "
             f"{position + 1})",
             f"хатка {index} сдвинулась на {abs(offset)} строк "
             f"{'позже' if offset > 0 else 'раньше'} (заявлено {hunk.old_start}, применено "
             f"{position + 1})")


def _locate(label: str, hunk: _Hunk, norm: list[str]) -> tuple[int, int, str]:
    """Where the hunk belongs: (index into `norm`, offset from the hint, reason).

    The declared line is tried first, because that is the answer the model
    expects and the cheapest to verify. Only when it fails is the file searched
    for the same block, and only a single hit counts. Zero hits and several hits
    are both refusals: this tool moves a hunk when the bytes prove where it goes,
    never when they merely look similar.
    """
    if hunk.old_count == 0:
        # `@@ -N,0 +M,K @@` carries no context to check: the position is the
        # whole claim, so it is honoured exactly as written or refused outright.
        position = hunk.old_start
        if position == 0 and norm:
            return -1, 0, L(f"hunk {hunk.name()} claims {label} has no lines before it, but the "
                            f"file has {len(norm)}: write the hunk against those lines, with the "
                            f"real line number after the `-`",
                            f"хатка {hunk.name()} утверждает, что до неё в {label} строк нет, но "
                            f"их {len(norm)}: пиши хатку по этим строкам, с настоящим номером "
                            f"после `-`")
        if position > len(norm):
            return -1, 0, L(f"hunk {hunk.name()} inserts after line {hunk.old_start}, but "
                            f"{label} has only {len(norm)} line(s)",
                            f"хатка {hunk.name()} вставляет после строки {hunk.old_start}, а в "
                            f"{label} всего {len(norm)} строк")
        return position, 0, ""
    declared = hunk.old_start - 1
    if _matches(norm, declared, hunk.before):
        return declared, 0, ""
    hits = [p for p in range(len(norm) - len(hunk.before) + 1)
            if _matches(norm, p, hunk.before)]
    if len(hits) == 1:
        return hits[0], hits[0] - declared, ""
    if not hits:
        step, expected, found = _first_difference(norm, declared, hunk.before)
        return -1, 0, L(f"hunk {hunk.name()} does not match {label} at line "
                        f"{declared + step + 1}: the diff expects {expected}, the file has "
                        f"{found}, and this block of {len(hunk.before)} line(s) appears nowhere "
                        f"else in {label}. That context is invented or stale - read the file and "
                        f"rebuild the hunk",
                        f"хатка {hunk.name()} не совпадает с {label} в строке "
                        f"{declared + step + 1}: diff ждёт {expected}, в файле {found}, и этот "
                        f"блок из {len(hunk.before)} строк больше нигде в {label} не "
                        f"встречается. Такой контекст выдуман или устарел — прочитай файл и "
                        f"перестрой хатку")
    return -1, 0, L(f"hunk {hunk.name()} matches {len(hits)} places in {label} (lines "
                    f"{', '.join(str(p + 1) for p in hits)}), so it is refused instead of "
                    f"guessed at. Add context lines until the hunk names one spot",
                    f"хатка {hunk.name()} совпадает с {len(hits)} местами в {label} "
                    f"(строки {', '.join(str(p + 1) for p in hits)}), поэтому отказываемся, а не "
                    f"угадываем. Добавь строк контекста, чтобы хатка указывала на одно место")


def _first_difference(norm: list[str], declared: int, block: list[str]):
    """The first line where the hunk and the file part company, for the report.

    Naming the line that actually disagrees is what lets a model fix one hunk
    instead of guessing at the whole file, so it is worth the extra walk.
    """
    for step, expected in enumerate(block):
        at = declared + step
        if 0 <= at < len(norm) and norm[at] == expected:
            continue
        found = (repr(norm[at]) if 0 <= at < len(norm)
                 else L("nothing, because the file ends there",
                        "ничего, потому что файл там заканчивается"))
        return step, repr(expected), found
    return 0, repr(block[0] if block else ""), L("no such line", "нет такой строки")


def _matches(norm: list[str], start: int, block: list[str]) -> bool:
    if start < 0 or start + len(block) > len(norm):
        return False
    for step, text in enumerate(block):
        if norm[start + step] != text:
            return False
    return True


def _prevailing(raw_lines: list[str]) -> str:
    """The ending new lines get, which is the ending the file already uses.

    A new or empty file has no endings of its own, and the diff arrives LF-only
    through JSON, so `\n` is the honest answer there. A mixed file keeps its own
    lines verbatim and gets the majority ending on the lines the hunk writes.
    """
    crlf = sum(1 for line in raw_lines if line.endswith("\r\n"))
    lf = sum(1 for line in raw_lines if line.endswith("\n") and not line.endswith("\r\n"))
    return "\r\n" if crlf > lf else "\n"


def _split_lines(text: str) -> list[str]:
    """Split on LF / CRLF / CR only, keeping each ending attached to its line."""
    out: list[str] = []
    start = i = 0
    n = len(text)
    while i < n:
        char = text[i]
        if char == "\n":
            out.append(text[start:i + 1])
            i += 1
        elif char == "\r":
            i += 2 if text[i + 1:i + 2] == "\n" else 1
            out.append(text[start:i])
        else:
            i += 1
            continue                    # still inside the same line: do not move `start`
        start = i
    if start < n:
        out.append(text[start:])
    return out


def _text_of(line: str) -> str:
    """The line without exactly one trailing ending — what the comparison sees."""
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def _stamp(path: Path) -> tuple:
    """(modified, size, inode) — enough to notice a save we did not make."""
    try:
        info = path.stat()
    except OSError:
        return ()
    return (info.st_mtime_ns, info.st_size, getattr(info, "st_ino", 0))


# ------------------------------------------------------------- report & write ---

def _refused(what: str, reasons: list[str], pending: list[str]) -> ToolResult:
    """The one answer shape for "this diff did not run"."""
    lines = [L(f"`patch` REFUSED - {what}:", f"`patch` ОТКАЗАЛ — {what}:")]
    lines.extend(f"  - {reason}" for reason in reasons)
    if pending:
        lines.append(L(f"These {len(pending)} file(s) were ready and stay untouched anyway, "
                       f"because one diff is one transaction: {', '.join(pending)}",
                       f"Эти готовые файлы ({len(pending)}) тоже не тронуты: один diff — одна "
                       f"сделка: {', '.join(pending)}"))
    lines.append(L("No file was written. Fix the diff and send it again in full.",
                   "Ни один файл не записан. Исправь diff и пришли его целиком заново."))
    return ToolResult(output="\n".join(lines), error=True,
                      metadata={"refused": "diff", "reasons": reasons})


def _commit(jobs: list[_Job]) -> ToolResult:
    """The only place in this module that touches the disk."""
    # Every stamp again before the first write: the plan was built from bytes read
    # a moment ago, and a save that landed since then means the hunks were
    # verified against a file that no longer exists.
    for job in jobs:
        if _stamp(job.target) != job.stamp:
            return ToolResult(
                output=L(f"`patch` REFUSED - {job.label} changed while its hunks were being "
                         f"verified, so nothing at all was written. Read the files again and "
                         f"resend the whole diff",
                         f"`patch` ОТКАЗАЛ — {job.label} изменился, пока его хатки "
                         f"проверялись, поэтому ничего не записано. Прочитай файлы заново и "
                         f"пришли весь diff"),
                error=True, metadata={"refused": "changed-during-apply"})

    lines: list[str] = []
    broke: list[str] = []
    total_bytes = 0
    shifts: list[str] = []
    for job in jobs:
        shifts.extend(f"{job.label}: {note}" for note in job.shifts)
        try:
            if job.action == "delete":
                job.target.unlink()
                forget(job.target)
                job.written = 0
                outcome = L(f"the file's {job.old_bytes} bytes are gone from disk",
                            f"{job.old_bytes} байт файла исчезли с диска")
            else:
                job.target.parent.mkdir(parents=True, exist_ok=True)
                job.written = write_text_preserving(job.target, job.content)
                remember(job.target)
                outcome = L(f"{job.written} bytes written", f"записано {job.written} байт")
        except Exception as e:                       # noqa: BLE001 - reported per file
            broke.append(job.label)
            lines.append(L(f"  {job.label}: FAILED to write - {e}",
                           f"  {job.label}: НЕ ЗАПИСАН - {e}"))
            continue
        total_bytes += job.written
        verb = L("created", "создан") if job.action == "new" else \
            (L("modified", "изменён") if job.action == "modify" else L("removed", "удалён"))
        lines.append(L(f"  {job.label}: {verb}, {len(job.notes)} hunk(s), {outcome}",
                       f"  {job.label}: {verb}, хаток {len(job.notes)}, {outcome}"))
        lines.extend(f"    {note}" for note in job.notes)

    if broke:
        landed = ", ".join(job.label for job in jobs if job.label not in broke) or \
            L("none", "ни одного")
        return ToolResult(
            output="\n".join(lines) + "\n" + L(
                f"`patch` PARTIALLY APPLIED - {', '.join(broke)} could not be written after "
                f"{landed} already changed on disk. The undo journal holds the previous bytes of "
                f"every file listed, and this diff did not do the job you asked for: verify each "
                f"file before continuing",
                f"`patch` применён ЧАСТИЧНО - {', '.join(broke)} записать не удалось, а "
                f"{landed} уже изменены на диске. Прежние байты каждого перечисленного файла "
                f"есть в журнале отмены; этот diff не сделал то, что ты просил: проверь каждый "
                f"файл, прежде чем продолжать"),
            error=True, metadata={"refused": "partial-write", "partial": broke,
                                  "files": _facts(jobs)})

    verdict = L(f"patch applied: {len(jobs)} file(s), {sum(len(j.notes) for j in jobs)} hunk(s), "
                f"{total_bytes} bytes written",
                f"патч применён: файлов {len(jobs)}, хаток {sum(len(j.notes) for j in jobs)}, "
                f"записано байт: {total_bytes}")
    if shifts:
        verdict += L(f" - {len(shifts)} hunk(s) applied at an OFFSET, listed above",
                     f" - {len(shifts)} хатка(ок) применены СО СМЕЩЕНИЕМ, см. список выше")
    return ToolResult(output="\n".join(lines) + "\n" + verdict, error=False,
                      metadata={"bytes": total_bytes, "files": _facts(jobs),
                                "offsets": shifts})


def _facts(jobs: list[_Job]) -> list[dict]:
    return [{"path": str(job.target), "action": job.action, "hunks": len(job.notes),
             "bytes": job.written, "bytes_before": job.old_bytes} for job in jobs]
