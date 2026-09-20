"""Regex search over files.

Walk is lazy and pruned: `rglob("*")` materialised the whole tree before the
first file was read, so pointing this at a repository with a .git or a
node_modules paid for every path in it — and `.git` objects then produced
matches nobody asked for.
"""
import fnmatch
import os
import re
from pathlib import Path

from .base import BaseTool, ToolResult

MAX_FILES = 200
MAX_MATCHES = 50
SKIP_DIRS = {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv",
             "venv", ".beeagent", ".mypy_cache", "dist", "build"}


class GrepTool(BaseTool):
    name = "grep"
    description = (
        "Search file contents with a regular expression (log.*Error, class Foo) and return "
        "path:line matches. Filter filenames with include, e.g. *.py. Prefer this over running "
        "grep or rg through bash."
    )
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
            "include": {"type": "string", "description": "File name filter, e.g. *.py"},
        },
        "required": ["pattern", "path"],
    }

    def _files(self, root: Path, include: str | None):
        if root.is_file():
            yield root
            return
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for name in sorted(filenames):
                full = Path(dirpath) / name
                if include and not (fnmatch.fnmatch(full.as_posix(), include)
                                    or fnmatch.fnmatch(name, include)):
                    continue
                yield full

    def execute(self, pattern: str, path: str, include: str = None) -> ToolResult:
        try:
            p = Path(str(path)).expanduser()
            if not p.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)

            regex = re.compile(pattern)
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}", error=True)

        matches: list[str] = []
        scanned = 0
        unreadable = 0
        stopped = False

        for f in self._files(p, include):
            if scanned >= MAX_FILES:
                stopped = True
                break
            scanned += 1
            try:
                lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                unreadable += 1
                continue
            for i, line in enumerate(lines, 1):
                if regex.search(line):
                    matches.append(f"{f}:{i}: {line}")

        limit_note = (f"… stopped after the first {MAX_FILES} files "
                      f"(narrow the path or pass include)") if stopped else ""
        if not matches:
            return ToolResult(output="No matches found" + (f"\n{limit_note}" if limit_note else ""),
                              error=False, metadata={"count": 0, "scanned": scanned})

        shown = matches[:MAX_MATCHES]
        output = "\n".join(shown)
        notes = []
        if len(matches) > len(shown):
            notes.append(f"… and {len(matches) - len(shown)} more matches not shown")
        if unreadable:
            notes.append(f"… {unreadable} files could not be read as text")
        if limit_note:
            notes.append(limit_note)
        if notes:
            output += "\n" + "\n".join(notes)
        return ToolResult(output=output, error=False,
                          metadata={"count": len(matches), "scanned": scanned})

    def is_safe(self) -> bool:
        return True
