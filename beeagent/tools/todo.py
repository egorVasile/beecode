import json
from pathlib import Path
from .base import BaseTool, ToolResult

TODO_FILE = ".beeagent/todo.json"

class TodoTool(BaseTool):
    name = "todo"
    description = "Manage a task list (add, list, done, remove)"
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["add", "list", "done", "remove"]},
            "text": {"type": "string", "description": "Task text (for add)"},
            "id": {"type": "integer", "description": "Task id (for done/remove)"},
        },
        "required": ["action"],
    }
    
    def _load(self) -> list[dict]:
        p = Path(TODO_FILE)
        if p.exists():
            return json.loads(p.read_text())
        return []
    
    def _save(self, tasks: list[dict]):
        p = Path(TODO_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(tasks, indent=2))
    
    def execute(self, action: str, text: str = "", id: int = 0) -> ToolResult:
        tasks = self._load()
        if action == "add":
            task_id = max((t["id"] for t in tasks), default=0) + 1
            tasks.append({"id": task_id, "text": text, "done": False})
            self._save(tasks)
            return ToolResult(output=f"Added task #{task_id}", error=False)
        elif action == "list":
            if not tasks:
                return ToolResult(output="No tasks", error=False)
            lines = []
            for t in tasks:
                mark = "x" if t["done"] else " "
                lines.append(f"[{mark}] #{t['id']}: {t['text']}")
            return ToolResult(output="\n".join(lines), error=False)
        elif action == "done":
            for t in tasks:
                if t["id"] == id:
                    t["done"] = True
                    self._save(tasks)
                    return ToolResult(output=f"Task #{id} done", error=False)
            return ToolResult(output=f"Task #{id} not found", error=True)
        elif action == "remove":
            tasks = [t for t in tasks if t["id"] != id]
            self._save(tasks)
            return ToolResult(output=f"Removed task #{id}", error=False)
        return ToolResult(output=f"Unknown action: {action}", error=True)
    
    def is_safe(self) -> bool:
        return True