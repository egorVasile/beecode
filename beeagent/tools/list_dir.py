"""Directory listing.

Free models keep inventing `read_directory` / `list_files` / `read_files` for
this operation and `glob` cannot answer them — it only returns files. The
invented names are registered as aliases so the call still works, while the
catalog advertises the single canonical `list_directory`.
"""
import os
from pathlib import Path

from beeagent.i18n import L

from ._path_policy import guard
from .base import BaseTool, ToolResult

MAX_ENTRIES = 300
# Counting past the cap is what makes "… and N more" a number instead of a
# guess (it used to print "1 more" for 50).  A recursive walk pays a directory
# read for every extra entry, so the count stops at this budget and says
# "at least"; a flat listing has no such cost — `iterdir` read the whole
# directory before the first name was handed over — so it is counted in full.
MAX_SCANNED = 1200
COUNT_ALL = 10 ** 9
# A listing that walks a venv or a .git spends minutes stating files nobody
# asked about — and the old code did it before the first line was printed.
SKIP_DIRS = {".git", ".beeagent", "node_modules", "__pycache__", ".venv", "venv",
             "env", ".tox", ".mypy_cache", ".pytest_cache", ".idea", ".vscode"}


def _size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f}K"
    return f"{n / 1024 / 1024:.1f}M"


class ListDirectoryTool(BaseTool):
    name = "list_directory"
    aliases = ("read_directory", "read_files", "list_files", "list_dir")
    description = (
        "List a directory: subdirectories end with '/', files show their size. Use this to see "
        "what is in a folder, `read` for one file, and check a parent with it before creating a "
        "new directory inside."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Directory to list (e.g. . or C:/project)"},
            "recursive": {
                "type": "boolean",
                "description": "Walk into subdirectories too (capped at 300 entries)",
                "default": False,
            },
        },
        "required": ["path"],
    }

    def execute(self, path: str, recursive: bool = False) -> ToolResult:
        folder, refusal = guard(path, "list_directory")
        if refusal:
            return ToolResult(output=refusal, error=True,
                              metadata={"refused": "outside-working-directory"})
        try:
            if not folder.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)
            if not folder.is_dir():
                return ToolResult(
                    output=L(f"Not a directory: {path} — use the read tool for a file",
                             f"{path} — не каталог: для файла нужен read"),
                    error=True
                )

            # Counting past the cap is what makes "… and N more" a real number.
            # A flat listing has already read the whole directory, so the rest of
            # the count is free; a recursive one pays a directory read per entry
            # and stops at the budget, saying "at least" when it does.
            budget = MAX_SCANNED if recursive else COUNT_ALL
            entries, not_shown, scanned, cut_short = [], 0, 0, False
            for item in _walk(folder, recursive):
                scanned += 1
                if len(entries) >= MAX_ENTRIES:
                    not_shown += 1
                    if scanned >= budget:
                        cut_short = True
                        break
                    continue
                try:
                    label = item.relative_to(folder).as_posix()
                except ValueError:      # symlink pointing outside the folder
                    label = item.name
                if item.is_dir():
                    entries.append(f"{label}/")
                else:
                    try:
                        entries.append(f"{label}  {_size(item.stat().st_size)}")
                    except OSError:
                        entries.append(label)

            if not entries:
                return ToolResult(output=f"{path} is empty", error=False, metadata={"count": 0})

            total = len(entries) + not_shown
            head = (f"{path}/ ({total} entries)" if not not_shown
                    else f"{path}/ (showing {len(entries)} of {total} entries)")
            lines = [head] + entries
            if not_shown:
                lines.append(L(f"… and {'at least ' if cut_short else ''}{not_shown} more not shown",
                               f"… и {'как минимум ' if cut_short else ''}{not_shown} не показано"))
            if cut_short:
                lines.append(L(f"… the count itself stopped at {MAX_SCANNED} entries, so there "
                               f"are at least {not_shown} more — this is a deep tree, name the "
                               f"folder you want",
                               f"… счёт тоже остановился на {MAX_SCANNED} записях, поэтому "
                               f"как минимум {not_shown} не учтены — дерево глубокое, назови "
                               f"нужную папку"))
            return ToolResult(
                output="\n".join(lines), error=False,
                metadata={"count": total, "shown": len(entries), "truncated": bool(not_shown)},
            )
        except OSError as e:
            return ToolResult(output=f"Cannot list {path}: {e}", error=True)
        except TypeError as e:
            return ToolResult(output=f"Bad arguments for list_directory: {e}", error=True)

    def is_safe(self) -> bool:
        return True


def _key(item: Path) -> tuple:
    return (item.is_file(), item.name.lower())


def _walk(folder: Path, recursive: bool):
    """Yield entries lazily, and never descend into another tool's output."""
    if not recursive:
        yield from sorted(folder.iterdir(), key=_key)
        return
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        base = Path(root)
        for name in sorted(dirs, key=str.lower):
            yield base / name
        for name in sorted(files, key=str.lower):
            yield base / name
