from pathlib import Path
from .base import BaseTool, ToolResult, read_text_preserving

class ReadTool(BaseTool):
    name = "read"
    description = (
        "Read a text file and return its lines numbered from 1. offset is a 0-based line index, "
        "limit is how many lines to return. Read a file before editing it; to see a folder use "
        "list_directory."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to read"},
            "offset": {"type": "integer", "description": "Starting line (0-indexed)", "default": 0},
            "limit": {"type": "integer", "description": "Max lines to read", "default": 2000},
        },
        "required": ["path"],
    }
    
    def execute(self, path: str, offset: int = 0, limit: int = 2000) -> ToolResult:
        try:
            p = Path(str(path)).expanduser()
            if not p.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            if p.is_dir():
                return ToolResult(
                    output=f"{path} is a directory — use list_directory to see its contents",
                    error=True,
                )
            lines = read_text_preserving(p).splitlines()
            selected = lines[offset:offset + limit]
            output = "\n".join(f"{i + offset + 1}: {line}" for i, line in enumerate(selected))
            rest = len(lines) - offset - len(selected)
            if rest > 0:
                output += f"\n… {rest} more lines (continue with offset={offset + len(selected)})"
            return ToolResult(output=output, error=False, metadata={"total_lines": len(lines)})
        except UnicodeDecodeError as e:
            # Saying "here is some text" about a run of U+FFFD is how a cp1251
            # file got rewritten as garbage: the model copies what it was shown.
            return ToolResult(
                output=f"{path} is not UTF-8 text ({e.reason} at byte {e.start}) — "
                       f"read it with bash if you must, and do not write it back",
                error=True)
        except OSError as e:
            return ToolResult(output=f"Cannot read {path}: {e}", error=True)
        except TypeError as e:
            return ToolResult(output=f"Bad arguments for read: {e}", error=True)
    
    def is_safe(self) -> bool:
        return True
