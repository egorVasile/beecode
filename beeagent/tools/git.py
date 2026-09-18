import subprocess
from .base import BaseTool, ToolResult

class GitTool(BaseTool):
    name = "git"
    description = "Execute git commands"
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Git command (without 'git' prefix)"},
        },
        "required": ["command"],
    }
    
    def execute(self, command: str) -> ToolResult:
        try:
            full_cmd = f"git {command}"
            result = subprocess.run(
                full_cmd, shell=True, capture_output=True, text=True, timeout=30
            )
            output = result.stdout
            if result.stderr:
                output += f"\n[stderr]\n{result.stderr}"
            return ToolResult(
                output=output or "(no output)",
                error=result.returncode != 0,
                metadata={"returncode": result.returncode},
            )
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False