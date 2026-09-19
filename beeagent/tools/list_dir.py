"""Directory listing.

Free models keep inventing `read_directory` / `list_files` / `read_files` for
this operation and `glob` cannot answer them — it only returns files. The
invented names are registered as aliases so the call still works, while the
catalog advertises the single canonical `list_directory`.
"""
from pathlib import Path

from .base import BaseTool, ToolResult

MAX_ENTRIES = 300


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
        "List a directory: subdirectories end with '/', files show their size. "
        "Use this to see what is in a folder; use `read` for one file."
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

            walker = folder.rglob("*") if recursive else folder.iterdir()
            entries, hidden = [], 0
            for item in sorted(walker, key=lambda f: (f.is_file(), f.name.lower())):
                if len(entries) >= MAX_ENTRIES:
                    hidden += 1
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
