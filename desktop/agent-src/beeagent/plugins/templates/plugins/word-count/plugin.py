"""Example extension: one plugin that uses every part of the API.

Copy this directory to `.beeagent/plugins/word-count/` and start BeeCode: the
model gets a `word_count` tool, the user gets `/wc`, the plugin gets a setting
it can tune in beeagent.json, and every answer is counted out loud.
"""
from beeagent.tools.base import BaseTool, ToolResult


class WordCountTool(BaseTool):
    name = "word_count"
    description = "Count words in a text or a file, and say how many lines it has."
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to count"},
            "path": {"type": "string", "description": "Or a file to count"},
        },
    }

    def execute(self, text: str = "", path: str = "") -> ToolResult:
        if path:
            try:
                text = open(path, encoding="utf-8").read()
            except OSError as e:
                return ToolResult(output=str(e), error=True)
        words = len(text.split())
        lines = len(text.splitlines())
        return ToolResult(output=f"{words} words in {lines} lines", error=False)

    def is_safe(self) -> bool:
        return True


TOOLS = [WordCountTool()]


def setup(api) -> None:
    """Called once at load. `api` is the whole surface a plugin may touch."""
    api.setting("echo", True, "print a note after every answer")

    def word_count(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        target = " ".join(args)
        if not target:
            return CommandResult(output=Text("usage: /wc <path>", style="dim"))
        result = WordCountTool().execute(path=target)
        return CommandResult(output=Text(result.output, style="#ffcc00"))

    api.command("wc", "Count words and lines in a file", word_count, usage="/wc <path>")

    def tally(event, data):
        if event == "done" and api.get("echo"):
            text = data.get("text") or ""
            if text.strip():
                print(f"  🐤 {len(text.split())} words in that answer")

    api.event("done", tally)
