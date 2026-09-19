from datetime import datetime
from pathlib import Path

from beeagent.tools.base import BaseTool, ToolResult

NOTES_PATH = Path(".beeagent") / "notes.md"


class NotesTool(BaseTool):
    name = "notes"
    description = (
        "Persistent quick notes. action=add appends a timestamped note; "
        "action=list shows all notes. Notes survive restarts."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list"], "description": "What to do"},
            "text": {"type": "string", "description": "Note text (for action=add)"},
        },
        "required": ["action"],
    }

    def execute(self, action: str = "list", text: str = "") -> ToolResult:
        if action == "add":
            if not text.strip():
                return ToolResult(output="Empty note — pass `text`.", error=True)
            NOTES_PATH.parent.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            with NOTES_PATH.open("a", encoding="utf-8") as f:
                f.write(f"- **{stamp}** {text.strip()}\n")
            return ToolResult(output=f"Saved. ({NOTES_PATH})", error=False)
        if action == "list":
            if not NOTES_PATH.exists():
                return ToolResult(output="(no notes yet)", error=False)
            return ToolResult(output=NOTES_PATH.read_text(encoding="utf-8"), error=False)
        return ToolResult(output=f"Unknown action '{action}'. Use add or list.", error=True)

    def is_safe(self) -> bool:
        return True


TOOLS = [NotesTool()]
