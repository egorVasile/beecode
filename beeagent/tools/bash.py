import subprocess
from .base import BaseTool, ToolResult
from .shell import run_text

# Models send this as "5", as null, as -5, or as 10**12. Each of those used to
# reach subprocess verbatim: a string crashed the tool, null disabled the
# timeout entirely, a negative number timed out instantly, and a huge one died
# in `timestamp out of range` — so a command the user allowed never ran.
def _seconds(value) -> int:
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        seconds = 60
    return max(1, min(seconds, 1800))

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
    
    def execute(self, command: str, timeout=60) -> ToolResult:
        seconds = _seconds(timeout)
        try:
            stdout, stderr, returncode = run_text(command, timeout=seconds)
            output = stdout
            if stderr:
                output += f"\n[stderr]\n{stderr}"
            return ToolResult(
                output=output or "(no output)",
                error=returncode != 0,
                metadata={"returncode": returncode},
            )
        except subprocess.TimeoutExpired:
            return ToolResult(output=f"Command timed out after {seconds}s", error=True)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False
