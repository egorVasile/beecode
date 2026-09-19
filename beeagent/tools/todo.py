import json
from pathlib import Path

from .base import BaseTool, ToolResult

TODO_FILE = ".beeagent/todo.json"

class TodoTool(BaseTool):
    name = "todo"
    description = (
        "Track a task list. Actions: add (needs text), list, done (needs id), "
        "remove (needs id). Ids come from list and start at 1."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "done", "remove"]},
            "text": {"type": "string", "description": "Task text (required for add)"},
            "id": {"type": "integer", "description": "Task id (required for done/remove)"},
        },
        "required": ["action"],
    }

    def _load(self) -> list[dict]:
        p = Path(TODO_FILE)
        if not p.exists():
            return []
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            # A hand-edited or half-written file must not kill the tool.
            return []
        return data if isinstance(data, list) else []

    def _save(self, tasks: list[dict]):
        p = Path(TODO_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tasks, indent=2), encoding="utf-8")

    def execute(self, action: str, text: str = "", id: int = 0) -> ToolResult:
        tasks = self._load()

        if action == "add":
            if not str(text).strip():
                return ToolResult(output="add needs a text argument", error=True)
            task_id = max((t.get("id", 0) for t in tasks), default=0) + 1
            tasks.append({"id": task_id, "text": str(text), "done": False})
            self._save(tasks)
            return ToolResult(output=f"Added task #{task_id}", error=False)

        if action == "list":
            if not tasks:
                return ToolResult(output="No tasks", error=False, metadata={"count": 0})
            lines = [
                f"[{'x' if t.get('done') else ' '}] #{t.get('id')}: {t.get('text')}"
                for t in tasks
            ]
            return ToolResult(output="\n".join(lines), error=False, metadata={"count": len(tasks)})

        if action in ("done", "remove"):
            if not id:
                return ToolResult(output=f"{action} needs an id (see action=list)", error=True)
            found = [t for t in tasks if t.get("id") == id]
            if not found:
                return ToolResult(output=f"Task #{id} not found", error=True)
            if action == "done":
                found[0]["done"] = True
            else:
                tasks = [t for t in tasks if t.get("id") != id]
            self._save(tasks)
            verb = "done" if action == "done" else "Removed"
            return ToolResult(output=f"{verb} task #{id}", error=False)

        return ToolResult(output=f"Unknown action: {action} (add|list|done|remove)", error=True)

    def is_safe(self) -> bool:
        return True
