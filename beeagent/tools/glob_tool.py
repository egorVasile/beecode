from pathlib import Path
from .base import BaseTool, ToolResult

class GlobTool(BaseTool):
    name = "glob"
    description = (
        "Find files by name pattern under a directory, e.g. **/*.py or src/**/*.ts. Returns "
        "files only — use list_directory to see a folder. When unsure where something lives, "
        "search several plausible patterns in one answer instead of guessing one at a time."
    )
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
            p = Path(str(path)).expanduser()
            if not p.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)
            files = sorted(str(f.relative_to(p)) for f in p.glob(str(pattern)) if f.is_file())
            if not files:
                return ToolResult(output="No files found", error=False, metadata={"count": 0})
            shown = files[:100]
            output = "\n".join(shown)
            if len(files) > len(shown):
                output += f"\n… and {len(files) - len(shown)} more files not shown"
            return ToolResult(output=output, error=False, metadata={"count": len(files)})
        except (OSError, TypeError, ValueError) as e:
            return ToolResult(output=f"glob failed: {e}", error=True)
    
    def is_safe(self) -> bool:
        return True