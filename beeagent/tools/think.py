"""Structured planning step: the model writes down its reasoning before acting.

The old heuristic (`_PROMISE_TO_ACT` regex) waited until the model promised
to do something and then sent a nudge.  This replaces guessing with a real
tool: the model calls `think` with its plan, the plan is stored and shown
to the user, and every later turn carries a `# PLAN` reminder in the system
prompt so the model stays on track.
"""
import json
from pathlib import Path

from .base import BaseTool, ToolResult

PLAN_FILE = ".beeagent/plan.json"


class ThinkTool(BaseTool):
    name = "think"
    description = (
        "Write down your plan before executing it.  Use this for any task with "
        "three or more steps.  Steps are shown to the user and persist across "
        "turns so you never lose track of where you are."
    )
    parameters = {
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": "Your step-by-step plan.  Be specific: name the files, "
                               "the commands, the expected outcome of each step.",
            },
            "status": {
                "type": "string",
                "enum": ["reasoning", "approved", "in_progress", "done"],
                "description": "Current status of this plan",
            },
        },
        "required": ["plan"],
    }
    writes_files = True
    _plan: dict = {"steps": [], "summary": ""}

    @classmethod
    def current(cls) -> dict:
        return dict(cls._plan)

    @classmethod
    def restore(cls, workdir: str = ".") -> dict:
        path = Path(workdir) / PLAN_FILE
        if path.is_file():
            try:
                cls._plan = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                cls._plan = {"steps": [], "summary": ""}
        return cls.current()

    def execute(self, plan: str, status: str = "reasoning") -> ToolResult:
        self._plan["summary"] = plan
        self._plan["steps"] = [s.strip() for s in plan.split("\n") if s.strip()]
        self._plan["status"] = status
        try:
            path = Path.cwd() / PLAN_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._plan, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            pass
        return ToolResult(
            output=f"[PLAN] status={status}\n{plan}",
            error=False,
            metadata={"plan_steps": len(self._plan["steps"])},
        )

    def is_safe(self) -> bool:
        return True
