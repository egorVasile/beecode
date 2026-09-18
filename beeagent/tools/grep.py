import re
from pathlib import Path
from .base import BaseTool, ToolResult

class GrepTool(BaseTool):
    name = "grep"
    description = "Search file contents using regex"
    parameters = {
        "type": "object",
        "properties": {
            "pattern": {"type": "string", "description": "Regex pattern"},
            "path": {"type": "string", "description": "Directory or file to search"},
            "include": {"type": "string", "description": "File pattern filter (e.g. *.py)"},
        },
        "required": ["pattern", "path"],
    }
    
    def execute(self, pattern: str, path: str, include: str = None) -> ToolResult:
        try:
            p = Path(path)
            if not p.exists():
                return ToolResult(output=f"Path not found: {path}", error=True)
            
            regex = re.compile(pattern)
            matches = []
            
            if p.is_file():
                files = [p]
            else:
                files = list(p.rglob("*"))
                if include:
                    files = [f for f in files if f.match(include)]
                files = [f for f in files if f.is_file()]
            
            for f in files[:100]:
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            matches.append(f"{f}:{i}: {line}")
                except Exception:
                    continue
            
            if not matches:
                return ToolResult(output="No matches found", error=False)
            return ToolResult(output="\n".join(matches[:50]), error=False, metadata={"count": len(matches)})
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}", error=True)
    
    def is_safe(self) -> bool:
        return True