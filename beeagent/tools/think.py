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
    # Working (non-think) tool calls since the plan was recorded. The HUD
    # reads it to show movement; the model moves it by working, not planning.
    _work_calls: int = 0

    @classmethod
    def note_work(cls) -> None:
        cls._work_calls += 1

    @classmethod
    def hud(cls) -> list[str]:
        """Compact plan rows for the strip above the prompt.

        Steps are numbered; the head row names the status and how many working
        calls happened since the plan — done-ness per step is the model's own
        `status` field (`done` celebrates), because only the model knows what
        "finished" means for its plan.
        """
        steps = list(cls._plan.get("steps") or [])
        if not steps:
            return []
        status = str(cls._plan.get("status") or "reasoning")
        head = f"plan [{status}]: {len(steps)} steps, {cls._work_calls} tool calls in"
        rows = [head]
        for number, step in enumerate(steps[:8], 1):
            rows.append(f"  {number}. {step[:100]}")
        if len(steps) > 8:
            rows.append(f"  … +{len(steps) - 8} more")
        if status == "done":
            rows.append("  done — nice.")
        return rows

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
        type(self)._work_calls = 0
        try:
            path = Path.cwd() / PLAN_FILE
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._plan, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        except OSError:
            pass
        # The plan is recorded. What the result says next decides whether
        # the model acts or re-plans — and echoing the whole plan back is
        # what kept it re-planning: it read its own words and refined them
        # instead of executing step 1. So the result confirms in one line
        # (the plan already stands in the call above) and orders action.
        # A second think before anything ran is how the think-loop breaker
        # upstairs gets tripped.
        steps = len(self._plan["steps"])
        first = self._plan["steps"][0] if self._plan["steps"] else ""
        return ToolResult(
            output=f"[PLAN] recorded: {steps} step(s), status={status}. "
                   f"First step: {first[:160]}\n"
                   f"[NEXT] execute it with a working tool call "
                   f"(list_directory, read, write, edit, bash, ...) in your "
                   f"next message. Do not call think again until step 1 has run.",
            error=False,
            metadata={"plan_steps": steps},
        )

    def is_safe(self) -> bool:
        return True
