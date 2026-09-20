import subprocess
from .base import BaseTool, ToolResult
from .shell import run_text

class GitTool(BaseTool):
    name = "git"
    description = (
        "Run git — pass the command without the leading 'git'. Commit, push, amend or open PRs "
        "only when the user asks: look at status, diff and recent log first, stage only intended "
        "files, never commit secrets, force-push, skip hooks or rewrite published history."
    )
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