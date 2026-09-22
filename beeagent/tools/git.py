import shlex
from .base import BaseTool, ToolResult
from .shell import run_argv_text

# The tool is granted as `git`, and a grant that reads "may use version control"
# must not be a grant to run anything at all. So the command is split into an argv
# and never reaches a shell: no `;`, no `&&`, no pipe, no `$(…)`.
_REJECTED = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n")


def split_command(command: str) -> list:
    """`git`'s own arguments, or a ValueError if the string wants a shell."""
    text = (command or "").strip()
    if not text:
        raise ValueError("no git command given")
    for shape in _REJECTED:
        if shape in text:
            raise ValueError(
                f"{shape.strip() or 'a newline'} is not allowed — the git tool runs git alone, "
                "not a shell. Chain steps with separate calls")
    return ["git"] + shlex.split(text, posix=True)


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
            argv = split_command(command)
        except ValueError as e:
            return ToolResult(output=f"refused: {e}", error=True)
        try:
            stdout, stderr, returncode = run_argv_text(argv, timeout=30)
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