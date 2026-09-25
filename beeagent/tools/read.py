"""`read` — the tool the model quotes its edits from.

Two things used to make that unsafe: the bytes shown could be the wrong bytes
(a file saved by the user during the read, a path that was really a symlink out
of the project), and one enormous line came back whole, which is not an excerpt
of a file but the file.
"""
from beeagent.i18n import L

from ._path_policy import guard
from ._seen import remember, stamp_of
from .base import BaseTool, ToolResult, read_text_preserving


class ReadTool(BaseTool):
    name = "read"
    description = (
        "Read a text file and return its lines numbered from 1. offset is a 0-based line index, "
        "limit is how many lines to return. Read a file before editing it; to see a folder use "
        "list_directory. Paths outside the working directory are refused."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read (inside the working directory)"},
            "offset": {"type": "integer", "description": "Starting line (0-indexed)", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    }

    MAX_LINES = 2000
    # A minified bundle or a CSV is often ONE line of megabytes. Handing it over
    # whole is not an excerpt, and `offset` cannot walk inside it, so the line is
    # cut and the model is told exactly what it did not see.
    MAX_LINE_CHARS = 2000
    MAX_OUTPUT_CHARS = 200_000

    def execute(self, path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
        try:
            # `limit` arrives from the model: an enormous value used to mean "read
            # the whole file, split it, number it and keep all of it in memory".
            limit = max(1, min(int(limit), self.MAX_LINES))
            offset = max(0, int(offset))
        except (TypeError, ValueError):
            return ToolResult(
                output=L("`offset` and `limit` must be whole numbers",
                         "`offset` и `limit` должны быть целыми числами"),
                error=True)
        target, refusal = guard(path, "read")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})
        try:
            if not target.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            if target.is_dir():
                return ToolResult(
                    output=f"{path} is a directory — use list_directory to see its contents",
                    error=True,
                )
            # The stamp is taken BEFORE the bytes are read: remembering the state
            # measured afterwards is how an edit raced a save that landed while we
            # were reading and still reported "Replaced".
            before = stamp_of(target)
            text = read_text_preserving(target)
            if stamp_of(target) != before:
                return ToolResult(
                    output=L(f"{path} changed while it was being read, so what is shown here "
                             f"is not the file — read it again",
                             f"{path} изменился во время чтения, то есть показанное — не файл; "
                             f"прочитай ещё раз"),
                    error=True)
            lines = text.splitlines()
            if lines and offset >= len(lines):
                # `''` with error=False read to the model as "the file ends here".
                return ToolResult(
                    output=L(f"offset {offset} is past the end of {path}: it has {len(lines)} "
                             f"lines (the last one is {len(lines)}). Nothing was returned.",
                             f"offset {offset} за концом файла {path}: в нём {len(lines)} "
                             f"строк (последняя — {len(lines)}). Ничего не возвращено."),
                    error=True, metadata={"total_lines": len(lines)})
            selected = lines[offset:offset + limit]
            body, used, cut = [], 0, 0
            for index, line in enumerate(selected):
                rendered = f"{index + offset + 1}: {line}"
                if len(line) > self.MAX_LINE_CHARS:
                    rendered = f"{index + offset + 1}: {line[:self.MAX_LINE_CHARS]}…"
                    cut += 1
                if used + len(rendered) + 1 > self.MAX_OUTPUT_CHARS:
                    break
                body.append(rendered)
                used += len(rendered) + 1
            shown = len(body)
            output = "\n".join(body)
            notes = []
            if cut:
                notes.append(L(
                    f"{cut} of the lines shown is longer than {self.MAX_LINE_CHARS} characters "
                    f"and was cut with … — the file still holds the whole line, this tool cannot "
                    f"show the rest of it (use bash or python to walk inside one long line)",
                    f"{cut} показанных строк длиннее {self.MAX_LINE_CHARS} символов и обрезаны "
                    f"многоточием — в файле строка целиком, здесь её остаток не показать "
                    f"(для длинной строки нужен bash или python)"))
            stopped = len(selected) - shown
            if stopped > 0:
                notes.append(L(f"… stopped after {shown} lines "
                               f"({self.MAX_OUTPUT_CHARS // 1000} KB of output) — continue with "
                               f"offset={offset + shown}",
                               f"… остановлено после {shown} строк "
                               f"({self.MAX_OUTPUT_CHARS // 1000} KB вывода) — продолжай с "
                               f"offset={offset + shown}"))
            rest = len(lines) - offset - shown
            if rest > 0:
                notes.append(f"… {rest} more lines (continue with offset={offset + shown})")
            if notes:
                output = output + "\n" + "\n".join(notes) if output else "\n".join(notes)
            remember(target)          # keyed by realpath: any later spelling matches
            return ToolResult(output=output, error=False,
                              metadata={"total_lines": len(lines), "truncated": bool(notes)})
        except UnicodeDecodeError as e:
            # Saying "here is some text" about a run of U+FFFD is how a cp1251
            # file got rewritten as garbage: the model copies what it was shown.
            return ToolResult(
                output=L(f"{path} is not UTF-8 text ({e.reason} at byte {e.start}) — "
                         f"read it with bash if you must, and do not write it back",
                         f"{path} — не текст в UTF-8 ({e.reason} на байте {e.start}): при "
                         f"необходимости читай его через bash и не пиши его обратно"),
                error=True)
        except OSError as e:
            return ToolResult(output=L(f"Cannot read {path}: {e}", f"Не удалось прочитать {path}: {e}"),
                              error=True)
        except TypeError as e:
            return ToolResult(output=L(f"Bad arguments for read: {e}", f"Неверные аргументы read: {e}"),
                              error=True)

    def is_safe(self) -> bool:
        return True
