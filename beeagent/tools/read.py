from pathlib import Path
from .base import BaseTool, ToolResult

class ReadTool(BaseTool):
    name = "read"
    description = "Read the contents of a file"
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
            p = Path(path)
            if not p.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            selected = lines[offset:offset + limit]
            output = "\n".join(f"{i + offset + 1}: {line}" for i, line in enumerate(selected))
            return ToolResult(output=output, error=False, metadata={"total_lines": len(lines)})
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return True
