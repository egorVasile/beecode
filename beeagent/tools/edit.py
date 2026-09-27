"""`edit` — one exact replacement in one file.

Every byte the model did not ask for has to survive the call, which is what the
stamp around the read is for: the copy in the conversation and the file on disk
are two things, and the tool may only write when they are still the same thing.
"""
import os

from beeagent.core import journal
from beeagent.i18n import L

from ._path_policy import guard
from ._seen import changed_since_read, remember
from .base import BaseTool, ToolResult, read_text_preserving, write_text_preserving


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Replace exact unique text in a file, read first. line_start/line_end "
        "scope lines; dry_run previews; edits batches several pairs."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "old_text": {"type": "string", "description": "Text to find"},
            "new_text": {"type": "string", "description": "Replacement"},
            "line_start": {"type": "integer",
                           "description": "First line"},
            "line_end": {"type": "integer",
                         "description": "Last line"},
            "dry_run": {"type": "boolean",
                        "description": "No write"},
            "edits": {"type": "array",
                      "description": "More pairs",
                      "items": {"type": "object"}},
        },
        "required": ["path"],
    }

    def execute(self, path: str, old_text: str = "", new_text: str = "",
                line_start=None, line_end=None, dry_run: bool = False,
                edits=None) -> ToolResult:
        try:
            target, refusal = guard(path, "edit")
            if refusal:
                return ToolResult(output=refusal, error=True,
                                  metadata={"refused": "outside-working-directory"})
            if not target.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            if not isinstance(old_text, str) or not isinstance(new_text, str):
                return ToolResult(
                    output=L("`old_text` and `new_text` must both be text",
                             "`old_text` и `new_text` должны быть текстом"), error=True)
            pairs = self._pairs(old_text, new_text, edits)
            if pairs is None:
                return ToolResult(
                    output=L("give old_text with new_text, or edits=[{old_text, new_text}, …] — "
                             "every edits entry needs string old_text and new_text",
                             "дай old_text с new_text или edits=[{old_text, new_text}, …] — "
                             "каждой записи нужны строковые old_text и new_text"),
                    error=True)
            # Both checks below speak to `target`, which `guard` resolved, so
            # reading `C:\proj\src\app.py` and editing `src/app.py` is one file to
            # the staleness table and not two.
            if changed_since_read(target):
                # The text the model is quoting is not the file any more.
                return ToolResult(
                    output=L(f"{path} changed since you read it. Read it again and "
                             f"apply the edit to the current text",
                             f"{path} изменился с тех пор, как ты его читал. Прочитай его "
                             f"заново и примени правку к текущему тексту"), error=True)
            # Taken before the content is read: stamping afterwards let a save
            # that landed during the read hide inside a stamp of its own making,
            # and the call answered "Replaced" over the user's new line.
            stamp = _stamp(target)
            content = read_text_preserving(target)
            if _stamp(target) != stamp:
                return _too_late(path)
            scope = self._scope(content, line_start, line_end, path)
            if isinstance(scope, ToolResult):
                return scope
            lo, hi, total_lines = scope
            # One pass per replacement over the evolving text, one write at the
            # end: five small fixes cost one round trip, one journal record and
            # one line of output instead of five of each.
            plan = []
            working = content
            for find, replace in pairs:
                find, replace = self._endings(working, find, replace)
                count = working.count(find, lo, hi if hi is not None else len(working))
                if count == 0:
                    return self._missed(path, working, find, lo, hi, total_lines)
                if count > 1:
                    return ToolResult(
                        output=L(f"Ambiguous: {count} matches found — narrow with "
                                 f"line_start/line_end or include more surrounding lines",
                                 f"Неоднозначно: найдено {count} совпадений — сузь "
                                 f"через line_start/line_end или добавь окружающие строки"),
                        error=True)
                at = working.index(find, lo, hi if hi is not None else len(working))
                plan.append((at, find, replace))
                working = working[:at] + replace + working[at + len(find):]
                # Later replacements address the text as it stands after the
                # earlier ones; the scope stays pinned to the original lines.
            if _stamp(target) != stamp:
                # Someone else saved this file between our read and our write —
                # their edits are in those bytes, and writing now would drop them
                # while reporting a clean "Replaced".
                return _too_late(path)
            ranges = ", ".join(
                self._lines_of(content, at, len(replace)) for at, _, replace in plan)
            if dry_run:
                return ToolResult(
                    output=L(f"Would replace lines {ranges} in {path} — nothing written",
                             f"Заменил бы строки {ranges} в {path} — ничего не записано"),
                    error=False, metadata={"dry_run": True, "lines": ranges})
            # The pre-image goes into the journal before the rename, so an edit
            # the user cannot take back is an edit that never happened. Every
            # refusal above leaves the journal untouched.
            try:
                saved = journal.record(os.getcwd(), "edit", path, action="modify")
            except journal.JournalError as e:
                return ToolResult(output=journal.refusal("edit", e), error=True,
                                  metadata={"refused": "undo-journal-not-recorded"})
            write_text_preserving(target, working)
            remember(target)      # the new bytes are now what we showed
            return ToolResult(
                output=L(f"Replaced lines {ranges} in {path}"
                         f"{journal.recoverable_clause(saved)}",
                         f"Изменены строки {ranges} в {path}"
                         f"{journal.recoverable_clause(saved)}"),
                error=False, metadata={"undo": saved, "lines": ranges})
        except Exception as e:
            return ToolResult(output=str(e), error=True)

    @staticmethod
    def _pairs(old_text, new_text, edits):
        """The replacement list, in order — or None when nothing valid was asked.

        An empty find would match everywhere, so it is not a replacement: it is
        refused like a missing one.
        """
        if edits is not None:
            if not isinstance(edits, list) or not edits:
                return None
            pairs = []
            for item in edits:
                if not isinstance(item, dict) \
                        or not isinstance(item.get("old_text"), str) \
                        or not item.get("old_text") \
                        or not isinstance(item.get("new_text"), str):
                    return None
                pairs.append((item["old_text"], item["new_text"]))
            return pairs
        if old_text:
            return [(old_text, new_text if isinstance(new_text, str) else "")]
        return None

    @staticmethod
    def _scope(content, line_start, line_end, path):
        """(lo, hi, total) char span for 1-based lines, or a refusal result."""
        lines = content.splitlines(keepends=True)
        total = len(lines)
        if line_start is None and line_end is None:
            return 0, None, total
        try:
            lo_line = int(line_start) if line_start is not None else 1
            hi_line = int(line_end) if line_end is not None else total
        except (TypeError, ValueError):
            return ToolResult(output=L("line_start and line_end must be line numbers",
                                       "line_start и line_end должны быть номерами строк"),
                              error=True)
        if lo_line < 1 or hi_line < lo_line:
            return ToolResult(
                output=L(f"bad line range {line_start}–{line_end} for {path} "
                         f"({total} lines)",
                         f"плохой диапазон строк {line_start}–{line_end} для {path} "
                         f"(всего {total})"), error=True)
        if lo_line > total:
            return ToolResult(
                output=L(f"{path} has {total} lines — line_start {lo_line} is past the end",
                         f"в {path} всего {total} строк — line_start {lo_line} за концом"),
                error=True)
        hi_line = min(hi_line, total)
        lo = sum(len(lines[i]) for i in range(lo_line - 1))
        hi = sum(len(lines[i]) for i in range(hi_line))
        return lo, hi, total

    @staticmethod
    def _endings(content, find, replace):
        if find and content.count(find) == 0 and "\r\n" in content:
            # The model read this file through splitlines(), which drops the
            # \r, so its fragment is LF-only while the file is CRLF. Match the
            # file's own endings rather than rewriting them.
            return find.replace("\n", "\r\n"), replace.replace("\n", "\r\n")
        return find, replace

    @staticmethod
    def _lines_of(content, offset, length) -> str:
        """'12-15' for a replacement: the numbers the read output showed."""
        first = content.count("\n", 0, offset) + 1
        last = first + content[offset:offset + length].count("\n")
        return f"{first}" if first == last else f"{first}-{last}"

    def _missed(self, path, content, find, lo, hi, total) -> ToolResult:
        """'Not found' with the nearest line that could be meant.

        A bare miss costs the model a full re-read; one line number pointing at
        the closest fragment usually costs one more call instead.
        """
        lines = content.splitlines()
        needle = next((ln.strip() for ln in find.splitlines() if ln.strip()),
                      find.strip()[:60])
        near = [str(n) for n, ln in enumerate(lines, 1)
                if needle and needle[:30] in ln][:3]
        scoped = f" within lines {content.count(chr(10), 0, lo) + 1}–" \
            if (lo or hi is not None) else ""
        hint = (f" — nearest similar text on line {', '.join(near)}"
                if near else "")
        return ToolResult(
            output=L(f"Text not found in {path}{scoped} ({total} lines){hint}",
                     f"Текст не найден в {path} ({total} строк){hint}"),
            error=True)

    def is_safe(self) -> bool:
        return False


def _too_late(path) -> ToolResult:
    return ToolResult(
        output=L(f"{path} changed since it was read. Read it again and apply the edit "
                 f"to the current text",
                 f"{path} изменился с момента чтения. Прочитай его заново и примени "
                 f"правку к текущему тексту"), error=True)


def _stamp(p) -> tuple:
    """(modified, size, inode) — enough to notice a save we did not make."""
    try:
        info = p.stat()
    except OSError:
        return ()
    return (info.st_mtime_ns, info.st_size, getattr(info, "st_ino", 0))
