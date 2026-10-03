"""`/goal` — one persistent objective for this project, across turns and restarts.

Codex's `/goal` keeps the agent working toward a stated target instead of
drifting after the third turn. BeeCode's loop reads the live `think` plan, not
a file — so this command is the durable half of that pair: the objective is
stored in `<workdir>/.beeagent/goal.json` (text, status, timestamp), survives
restarts, and is printed back with every change so the person sees what the
record holds. Saying "work toward the goal" in chat is what points the model
at it; the file is what keeps the words from changing between turns.

Subcommands: `/goal <text>` sets, `/goal` shows, `/goal pause|resume|done|clear`.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from beeagent.i18n import L

COMMAND_NAME = "goal"
USAGE = "/goal [text | pause | resume | done | clear]"
DESCRIPTION = "Set a persistent objective for this project"

GOAL_FILE = ".beeagent/goal.json"
ACTIVE, PAUSED, DONE = "active", "paused", "done"


def _path(workdir: str) -> Path:
    return Path(workdir) / GOAL_FILE


def current_goal(workdir: str = ".") -> dict:
    """The stored objective, or {} — the loop and the HUD read this, never crash."""
    try:
        body = json.loads(_path(workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) and body.get("text") else {}


def _write(workdir: str, text: str, status: str) -> None:
    path = _path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"text": text, "status": status,
                                "updated_at": time.time()},
                               ensure_ascii=False, indent=2),
                    encoding="utf-8")


def register_command() -> bool:
    """Put `/goal` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI and one-shot runs. Idempotent, core
    category so no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_goal:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="session")
    core.HANDLERS[COMMAND_NAME] = _cmd_goal
    return True


def _show(goal: dict):
    from rich.text import Text

    from beeagent.ui.commands import CommandResult

    if not goal:
        return CommandResult(output=Text(L(
            "no goal set — /goal <text> names the objective this project works toward.",
            "цель не задана — /goal <текст> назовёт цель, к которой идёт проект."),
            style="dim"))
    mark = {"active": "🎯", "paused": "⏸", "done": "✔"}.get(goal.get("status"), "•")
    return CommandResult(output=Text(
        f"{mark} [{goal.get('status')}] {goal.get('text')}\n"
        + L("say “work toward the goal” and the model aims at this.",
            "скажи «работай над целью» — и модель прицелится в неё."), style="bold"))


def _cmd_goal(ctx, args):
    from rich.text import Text

    from beeagent.ui.commands import CommandResult

    workdir = getattr(getattr(ctx, "agent", None), "workdir", None) or "."
    word = " ".join(args).strip()
    low = word.lower()

    if not word:
        return _show(current_goal(workdir))
    if low in ("pause", "hold", "stop"):
        goal = current_goal(workdir)
        if not goal:
            return CommandResult(output=Text(L(
                "no goal to pause — /goal <text> sets one first.",
                "паузить нечего — сначала /goal <текст>."), style="bold yellow"))
        try:
            _write(workdir, goal["text"], PAUSED)
        except OSError as e:
            return CommandResult(output=Text(str(e), style="bold red"))
        return CommandResult(output=Text(L(
            f"⏸ goal paused: {goal['text']}", f"⏸ цель на паузе: {goal['text']}"),
            style="bold yellow"))
    if low in ("resume", "continue", "unpause"):
        goal = current_goal(workdir)
        if not goal:
            return CommandResult(output=Text(L(
                "no goal to resume — /goal <text> sets one first.",
                "возобновлять нечего — сначала /goal <текст>."), style="bold yellow"))
        try:
            _write(workdir, goal["text"], ACTIVE)
        except OSError as e:
            return CommandResult(output=Text(str(e), style="bold red"))
        return CommandResult(output=Text(L(
            f"🎯 goal active again: {goal['text']}", f"🎯 цель снова активна: {goal['text']}"),
            style="bold green"))
    if low in ("done", "finish", "complete"):
        goal = current_goal(workdir)
        if not goal:
            return CommandResult(output=Text(L(
                "no goal to finish — /goal <text> sets one first.",
                "завершать нечего — сначала /goal <текст>."), style="bold yellow"))
        try:
            _write(workdir, goal["text"], DONE)
        except OSError as e:
            return CommandResult(output=Text(str(e), style="bold red"))
        return CommandResult(output=Text(L(
            f"✔ goal done: {goal['text']}", f"✔ цель выполнена: {goal['text']}"),
            style="bold green"))
    if low in ("clear", "reset", "remove", "drop"):
        try:
            _path(workdir).unlink(missing_ok=True)
        except OSError as e:
            return CommandResult(output=Text(str(e), style="bold red"))
        return CommandResult(output=Text(L(
            "goal cleared.", "цель убрана."), style="dim"))
    try:
        _write(workdir, word, ACTIVE)
    except OSError as e:
        return CommandResult(output=Text(
            L(f"could not store the goal: {e}", f"не смог сохранить цель: {e}"),
            style="bold red"))
    return CommandResult(output=Text(L(
        f"🎯 goal: {word}", f"🎯 цель: {word}"), style="bold green"))
