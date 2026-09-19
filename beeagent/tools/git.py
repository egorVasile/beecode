import subprocess
from .base import BaseTool, ToolResult
from .shell import run_text

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
            stdout, stderr, returncode = run_text(f"git {command}", timeout=30)
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            return ToolResult(
                output=output or "(no output)",
                error=returncode != 0,
                metadata={"returncode": returncode},
            )
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False