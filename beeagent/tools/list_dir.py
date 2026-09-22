"""Directory listing.

Free models keep inventing `read_directory` / `list_files` / `read_files` for
this operation and `glob` cannot answer them — it only returns files. The
invented names are registered as aliases so the call still works, while the
catalog advertises the single canonical `list_directory`.
"""
import os
from pathlib import Path

from .base import BaseTool, ToolResult

MAX_ENTRIES = 300
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
        try:
            folder = Path(str(path)).expanduser()
            if not folder.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)
            if not folder.is_dir():
                return ToolResult(
                    output=f"Not a directory: {path} — use the read tool for a file", error=True
                )

            entries, hidden = [], 0
            for item in _walk(folder, recursive):
                if len(entries) >= MAX_ENTRIES:
                    hidden += 1
                    break
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

            lines = [f"{path}/ ({len(entries)} entries)"] + entries
            if hidden:
                lines.append(f"… and {hidden} more not shown")
            return ToolResult(
                output="\n".join(lines), error=False, metadata={"count": len(entries)}
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
