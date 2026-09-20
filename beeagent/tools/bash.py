import subprocess
from .base import BaseTool, ToolResult
from .shell import run_text

class BashTool(BaseTool):
    name = "bash"
    description = (
        "Run a shell command: builds, tests, installs, docker, git plumbing. Do not use it for "
        "files — read, write, edit, grep, glob and list_directory exist for that. Quote paths "
        "that contain spaces, and chain steps that must happen in order with && ."
    )
    parameters = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "Shell command to execute"},
            "timeout": {"type": "integer", "description": "Timeout in seconds", "default": 60},
        },
        "required": ["command"],
    }
    
    def execute(self, command: str, timeout: int = 60) -> ToolResult:
        try:
            stdout, stderr, returncode = run_text(command, timeout=timeout)
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            return ToolResult(
                output=output or "(no output)",
                error=returncode != 0,
                metadata={"returncode": returncode},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"Command timed out after {timeout}s", error=True)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False
