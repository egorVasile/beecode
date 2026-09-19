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
            p = Path(str(path)).expanduser()
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
            
            scanned = files[:100]
            for f in scanned:
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                    for i, line in enumerate(lines, 1):
                        if regex.search(line):
                            matches.append(f"{f}:{i}: {line}")
                except Exception:
                    continue
            
            if not matches:
                return ToolResult(output="No matches found", error=False, metadata={"count": 0})
            shown = matches[:50]
            output = "\n".join(shown)
            if len(matches) > len(shown):
                output += f"\n… and {len(matches) - len(shown)} more matches not shown"
            elif len(files) > len(scanned):
                output += f"\n… only the first 100 of {len(files)} files were searched"
            return ToolResult(output=output, error=False, metadata={"count": len(matches)})
        except re.error as e:
            return ToolResult(output=f"Invalid regex: {e}", error=True)
        except (OSError, TypeError) as e:
            return ToolResult(output=f"grep failed: {e}", error=True)
    
    def is_safe(self) -> bool:
        return True