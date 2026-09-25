"""`glob` — names of files under a directory.

The pattern comes from the model, and `Path.glob` walks wherever it is told:
`../../*/secrets*` and `C:/Windows/**/*.ini` used to be answered with "No files
found" after the tool had gone looking, or with a list of files outside the
project.  Both are refused now, in words, and the walk is bounded.
"""
import os
import re
from pathlib import Path, PurePosixPath

from beeagent.i18n import L

from ._path_policy import guard, is_inside
from .base import BaseTool, ToolResult

# Collected, not printed: the count in "… and N more not shown" has to be one the
# walk actually paid for.
MAX_RESULTS = 2000
MAX_SHOWN = 100
MAX_OUTPUT_CHARS = 100_000
# A pattern that names a drive is not relative to anything: `C:/dir/*.py`.
_DRIVE = re.compile(r"^[A-Za-z]:")


class GlobTool(BaseTool):
    name = "glob"
    description = (
        "Find files by name pattern under a directory, e.g. **/*.py or src/**/*.ts. Returns "
        "files only — use list_directory to see a folder. The pattern is relative to that "
        "directory: it cannot carry \"..\" or a drive letter. When unsure where something "
        "lives, search several plausible patterns in one answer instead of guessing one at a "
        "time."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py), relative to the root"},
            "path": {"type": "string", "description": "Root directory to search (inside the working directory)"},
        },
        "required": ["pattern", "path"],
    }

    def execute(self, pattern: str, path: str) -> ToolResult:
        target, refusal = guard(path, "glob")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})
        if not isinstance(pattern, str):
            return ToolResult(
                output=L(f"`pattern` must be text, not {type(pattern).__name__} — a list is "
                         f"not a glob",
                         f"`pattern` должен быть текстом, а не {type(pattern).__name__}: "
                         f"список — это не glob"),
                error=True)
        text = pattern.strip()
        while text.startswith("./"):
            text = text[2:]
        if not text:
            return ToolResult(output=L("`pattern` is empty — nothing to look for",
                                       "`pattern` пуст — искать нечего"), error=True)
        pieces = PurePosixPath(text.replace("\\", "/")).parts
        if ".." in pieces or os.path.isabs(text) or _DRIVE.match(text):
            return ToolResult(
                output=L(f"`{pattern}` reaches outside the root it is given, and this tool "
                         f"searches only inside the working directory. Pass a pattern relative "
                         f"to `path`, e.g. **/*.py or src/**/*.py",
                         f"`{pattern}` уходит за пределы указанного корня, а эти инструменты "
                         f"ищут только внутри рабочей папки. Задай путь относительно `path`, "
                         f"например **/*.py или src/**/*.py"),
                error=True)
        try:
            found, escaped, truncated = [], 0, False
            for item in target.glob(text):
                try:
                    if not item.is_file():
                        continue
                except OSError:
                    continue
                if item.is_symlink() and not is_inside(item):
                    escaped += 1
                    continue
                if len(found) >= MAX_RESULTS:
                    truncated = True
                    break
                found.append(item)
            names = sorted(str(f.relative_to(target)).replace(os.sep, "/") for f in found)
            if not names:
                out = L("No files found", "Файлы не найдены")
                if escaped:
                    out += L(f" — {escaped} links point outside the working directory and were "
                             f"not listed",
                             f" — {escaped} ссылок ведут вне рабочей папки, они не "
                             f"показаны")
                return ToolResult(output=out, error=False, metadata={"count": 0})
            output, used, shown = "", 0, 0
            for name in names:
                if used + len(name) + 1 > MAX_OUTPUT_CHARS or shown >= MAX_SHOWN:
                    break
                output = name if not output else output + "\n" + name
                used += len(name) + 1
                shown += 1
            notes = []
            remaining = len(names) - shown
            if remaining > 0:
                notes.append(f"… and {remaining} more files not shown")
            if truncated:
                notes.append(L(f"… the walk itself stopped at {MAX_RESULTS} matches, so there "
                               f"are more files than this count",
                               f"… обход остановился на {MAX_RESULTS} совпадениях, файлов "
                               f"больше, чем показано в счёте"))
            if escaped:
                notes.append(L(f"… {escaped} matches are links out of the working directory "
                               f"and were not listed",
                               f"… {escaped} совпадений — ссылки наружу рабочей папки, они не "
                               f"показаны"))
            if notes:
                output += "\n" + "\n".join(notes)
            return ToolResult(output=output, error=False, metadata={"count": len(names)})
        except (OSError, TypeError, ValueError) as e:
            return ToolResult(output=L(f"glob failed: {e}", f"glob не удался: {e}"), error=True)

    def is_safe(self) -> bool:
        return True
