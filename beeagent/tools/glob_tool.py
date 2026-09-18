from pathlib import Path
from .base import BaseTool, ToolResult

class GlobTool(BaseTool):
    name = "glob"
    description = "Find files by glob pattern"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Glob pattern (e.g. **/*.py)"},
            "path": {"type": "string", "description": "Root directory to search"},
        },
        "required": ["pattern", "path"],
    }
    
    def execute(self, pattern: str, path: str) -> ToolResult:
        try:
            p = Path(path)
            if not p.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)
            files = sorted(str(f.relative_to(p)) for f in p.glob(pattern) if f.is_file())
            if not files:
                return ToolResult(output="No files found", error=False)
            return ToolResult(output="\n".join(files[:100]), error=False, metadata={"count": len(files)})
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return True