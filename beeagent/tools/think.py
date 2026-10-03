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
        "Write down your plan before a 3+ step task. Shown to the user, "
        "kept across turns."
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
        # The plan is recorded. What the result says next decides whether
        # the model acts or re-plans: a bare "saved" reads as permission to
        # plan again (seen live: plan, plan, plan, zero tool calls). So the
        # result orders step 1 now — a second think before anything ran is
        # how the think-loop breaker upstairs gets tripped.
        return ToolResult(
            output=f"[PLAN] status={status}\n{plan}\n"
                   f"[NEXT] execute step 1 with a working tool call "
                   f"(list_directory, read, write, edit, bash, ...) in your "
                   f"next message. Do not call think again until step 1 has run.",
            error=False,
            metadata={"plan_steps": len(self._plan["steps"])},
        )

    def is_safe(self) -> bool:
        return True
