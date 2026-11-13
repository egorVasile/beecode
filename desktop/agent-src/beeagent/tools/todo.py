import json
from pathlib import Path

from beeagent.i18n import L

from .base import BaseTool, ToolResult, read_text_preserving, write_text_preserving

TODO_FILE = ".beeagent/todo.json"


def _row(task: dict) -> str:
    """One task as the model and `/tasks` both show it."""
    return (f"[{'x' if task.get('done') else ' '}] #{task.get('id')}: "
            f"{task.get('text')}")


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

    def _read(self) -> tuple[list[dict], str]:
        """The plan, and the reason it cannot be trusted ('' when there is none).

        The old `_load` swallowed a bad file and returned `[]`, which is how a
        half-written plan vanished: `list` answered "No tasks", the model believed
        its plan was gone or empty, and the next `add` saved a fresh list over the
        user's bytes. A file that exists but cannot be read is not an empty plan,
        so the two cases come back apart and every caller has to say which one it
        is reporting.
        """
        p = Path(TODO_FILE)
        if not p.exists():
            # Nothing here yet: an honest empty list, and safe to save over.
            return [], ""
        try:
            raw = read_text_preserving(p)
        except (OSError, UnicodeDecodeError) as exc:
            return [], L(f"{TODO_FILE} could not be read ({exc})",
                         f"{TODO_FILE} не читается ({exc})")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            # A torn file is usually a save that died mid-write. Its bytes are the
            # user's plan, and only they can decide what to keep from them.
            return [], L(
                f"{TODO_FILE} is not valid JSON ({exc}). It looks like a save that "
                "was interrupted",
                f"{TODO_FILE} — не корректный JSON ({exc}). Похоже, сохранение "
                "прервалось")
        if not isinstance(data, list):
            return [], L(f"{TODO_FILE} holds {type(data).__name__}, not a list of tasks",
                         f"в {TODO_FILE} лежит {type(data).__name__}, а не список задач")
        rows = [row for row in data if isinstance(row, dict)]
        if len(rows) != len(data):
            broken = len(data) - len(rows)
            return rows, L(
                f"{broken} of {len(data)} lines in {TODO_FILE} are not task records "
                f"(expected {{'id', 'text', 'done'}} dicts); the other {len(rows)} "
                "were kept in memory",
                f"{broken} из {len(data)} строк в {TODO_FILE} — не записи задачи "
                f"(нужен словарь с 'id', 'text', 'done'); остальные {len(rows)} "
                "прочитаны")
        return rows, ""

    def _save(self, tasks: list[dict]) -> int:
        """Write the plan through the atomic helper; the byte count it reached."""
        p = Path(TODO_FILE)
        p.parent.mkdir(parents=True, exist_ok=True)
        # `path.write_text` truncated the plan before a single byte was verified:
        # a Ctrl+C or a full disk mid-save left a file too torn to read, and the
        # next turn's `_load` called that "No tasks".
        return write_text_preserving(p, json.dumps(tasks, indent=2) + "\n")

    def _refusal(self, problem: str) -> ToolResult:
        """What a write action says when it could not read the plan."""
        return ToolResult(
            output=L(f"Nothing was changed: {problem}. The tool will not save over a "
                     f"plan it could not read, because saving would replace the "
                     f"bytes that are still your work. Fix or remove {TODO_FILE} "
                     "first; `/tasks` shows what is in it.",
                     f"Ничего не изменено: {problem}. Инструмент не будет сохранять "
                     f"поверх плана, который не смог прочитать, — сохранение "
                     f"заменило бы байты, в которых ещё ваша работа. Сначала "
                     f"исправьте или удалите {TODO_FILE}; `/tasks` покажет, что в "
                     "нём."),
            error=True,
            metadata={"saved": False, "problem": problem})

    def execute(self, action: str, text: str = "", id: int = 0) -> ToolResult:
        tasks, problem = self._read()

        if action == "add":
            if not str(text).strip():
                return ToolResult(output="add needs a text argument", error=True)
            if problem:
                return self._refusal(problem)
            wanted = " ".join(str(text).split()).casefold()
            for existing in tasks:
                if " ".join(str(existing.get("text", "")).split()).casefold() == wanted:
                    # Measured on 2026-09-24: a model asked to "record the plan"
                    # re-added the same three items on every one of nine turns and
                    # finished nothing. An add that refuses to duplicate is the
                    # only defence that does not depend on the model cooperating.
                    return ToolResult(
                        output=(f"Task #{existing['id']} already says this "
                                f"({'done' if existing.get('done') else 'still open'}) — "
                                f"mark it with action=done instead of adding it again"),
                        error=False)
            task_id = max((t.get("id", 0) for t in tasks), default=0) + 1
            tasks.append({"id": task_id, "text": str(text), "done": False})
            return self._store(tasks, L(f"Added task #{task_id}",
                                        f"Добавлена задача #{task_id}"))

        if action == "list":
            if problem:
                # The rows that did read are still worth showing, but the answer
                # must not arrive as `error=False` while a file we cannot parse
                # sits on disk — that is how a lost plan looks like an empty one.
                parts = [problem]
                if tasks:
                    parts.append(L("what still reads:", "что ещё читается:"))
                    parts.extend(_row(t) for t in tasks)
                else:
                    parts.append(L("not one line of it reads as a task",
                                   "ни одна строка не читается как задача"))
                return ToolResult(
                    output="\n".join(parts),
                    error=True,
                    metadata={"count": len(tasks), "complete": False})
            if not tasks:
                return ToolResult(output="No tasks", error=False, metadata={"count": 0})
            lines = [_row(t) for t in tasks]
            return ToolResult(output="\n".join(lines), error=False, metadata={"count": len(tasks)})

        if action in ("done", "remove"):
            if not id:
                return ToolResult(output=f"{action} needs an id (see action=list)", error=True)
            if problem:
                return self._refusal(problem)
            found = [t for t in tasks if t.get("id") == id]
            if not found:
                return ToolResult(output=f"Task #{id} not found", error=True)
            if action == "done":
                found[0]["done"] = True
            else:
                tasks = [t for t in tasks if t.get("id") != id]
            said = (L(f"done task #{id}", f"задача #{id} отмечена выполненной")
                    if action == "done" else
                    L(f"Removed task #{id}", f"задача #{id} удалена"))
            return self._store(tasks, said)

        return ToolResult(output=f"Unknown action: {action} (add|list|done|remove)", error=True)

    def _store(self, tasks: list[dict], said: str) -> ToolResult:
        """Save, and report what actually happened to the bytes."""
        try:
            saved = self._save(tasks)
        except OSError as exc:
            # The write is atomic, so the plan on disk really is the one from
            # before this call — say that instead of raising past the tool and
            # letting the model assume its change went through.
            return ToolResult(
                output=L(f"NOT saved: {said} — {TODO_FILE} could not be written "
                         f"({exc}). The plan on disk is the one from before this "
                         "call",
                         f"НЕ СОХРАНЕНО: {said} — записать {TODO_FILE} не удалось "
                         f"({exc}). На диске лежит тот план, что был до этого "
                         "вызова"),
                error=True,
                metadata={"saved": False})
        return ToolResult(
            output=said + L(f" — {len(tasks)} in the plan, {saved} bytes written "
                            f"to {TODO_FILE}",
                            f" — в плане {len(tasks)}, в {TODO_FILE} записано "
                            f"{saved} байт"),
            error=False,
            metadata={"saved": True, "count": len(tasks), "bytes": saved})

    # It keeps its list in .beeagent/todo.json: safe to look with, but it
    # does write, which is what /permissions readonly promises to stop.
    writes_files = True

    def is_safe(self) -> bool:
        return True
