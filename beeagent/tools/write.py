from pathlib import Path
from .base import BaseTool, ToolResult

class WriteTool(BaseTool):
    name = "write"
    description = (
        "Create a new file or rewrite one completely (missing parent directories are created). "
        "Use edit for changes to existing code, and do not create files the task does not need — "
        "no notes, summaries or logs unless someone asked for them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }
    
    def execute(self, path: str, content: str) -> ToolResult:
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return ToolResult(output=f"Written {len(content)} bytes to {path}", error=False)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False
