"""`edit` — one exact replacement in one file.

Every byte the model did not ask for has to survive the call, which is what the
stamp around the read is for: the copy in the conversation and the file on disk
are two things, and the tool may only write when they are still the same thing.
"""
from beeagent.i18n import L

from ._path_policy import guard
from ._seen import changed_since_read, remember
from .base import BaseTool, ToolResult, read_text_preserving, write_text_preserving


class EditTool(BaseTool):
    name = "edit"
    description = (
        "Replace one exact, unique piece of text in a file. Read the file first and copy the text "
        "with its real indentation; when the match is ambiguous, include more surrounding lines. "
        "Prefer editing an existing file over creating a new one. Only inside the working "
        "directory: a path outside it is refused, never silently redirected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "old_text": {"type": "string", "description": "Exact text to find"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_text", "new_text"],
    }

    def execute(self, path: str, old_text: str, new_text: str) -> ToolResult:
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
            find, replace = old_text, new_text
            if content.count(find) == 0 and "\r\n" in content:
                # The model read this file through splitlines(), which drops the
                # \r, so its fragment is LF-only while the file is CRLF. Match the
                # file's own endings rather than rewriting them.
                find = old_text.replace("\n", "\r\n")
                replace = new_text.replace("\n", "\r\n")
            count = content.count(find)
            if count == 0:
                return ToolResult(output=L(f"Text not found in {path}",
                                           f"Текст не найден в {path}"), error=True)
            if count > 1:
                return ToolResult(output=L(f"Ambiguous: {count} matches found",
                                           f"Неоднозначно: найдено {count} совпадений"), error=True)
            if _stamp(target) != stamp:
                # Someone else saved this file between our read and our write —
                # their edits are in those bytes, and writing now would drop them
                # while reporting a clean "Replaced".
                return _too_late(path)
            write_text_preserving(target, content.replace(find, replace, 1))
            remember(target)      # the new bytes are now what we showed
            return ToolResult(output=L(f"Replaced in {path}", f"Изменено в {path}"), error=False)
        except Exception as e:
            return ToolResult(output=str(e), error=True)

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
