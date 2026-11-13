"""`/context` — what is filling the model's window, in one place.

Claude Code's `/context` and Codex's `/status` answer the same question a
person asks before `/compact`: is this session still healthy, or is the model
already forgetting early decisions? BeeCode had the numbers but in three
different commands (`/token` for the count, `/window` for the ceiling,
`/stats` for the spend). This one reads the same sources and prints the whole
picture: tokens against the request ceiling, where the window size came from,
how many messages are already folded into the digest, and what else rides
along (tools, skills, plan).
"""
from __future__ import annotations

COMMAND_NAME = "context"
USAGE = "/context"
DESCRIPTION = "What is filling the context window right now"


def register_command() -> bool:
    """Put `/context` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI and one-shot runs. Idempotent, core
    category so no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_context:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="info")
    core.HANDLERS[COMMAND_NAME] = _cmd_context
    return True


def _bar(pct: int, width: int = 20) -> str:
    filled = max(0, min(width, int(round(pct * width / 100))))
    return "█" * filled + "░" * (width - filled)


def _cmd_context(ctx, args):
    from rich.text import Text

    from beeagent.ui.commands import CommandResult

    if ctx.agent is None:
        return CommandResult(output=Text("No agent available.", style="bold red"))
    from beeagent.utils.tokens import count_tokens
    from beeagent.i18n import L

    context = ctx.agent.context
    session = ctx.session
    msgs = session.to_dicts() if session is not None else []
    built = context.build_messages(msgs, ctx.agent.tools.to_schemas())
    # Same ruler as /token: count the messages themselves, not the JSON
    # escaping the model never sees.
    n = sum(count_tokens(str(m.get("content") or ""), ctx.config.model) for m in built)
    limit = context.max_tokens
    pct = int(100 * n / limit) if limit else 0

    from beeagent.core import windows

    measured = windows.measured(ctx.config.model)
    source = (L("measured here", "замерено здесь") if measured
              else L("claimed by the name", "заявлено именем"))

    loader = getattr(ctx.agent, "plugins", None)
    skills_n = len(loader.skills) if loader is not None else 0
    tools_n = len(ctx.agent.tools.list_names())

    from beeagent.tools.think import ThinkTool

    plan = ThinkTool.current()
    plan_line = ""
    if plan.get("steps"):
        plan_line = L(f"plan [{plan.get('status', '?')}]: {len(plan['steps'])} steps",
                      f"план [{plan.get('status', '?')}]: шагов: {len(plan['steps'])}")

    text = Text()
    text.append(f"{_bar(pct)} {pct}%\n", style="bold #ffcc00" if pct < 80 else "bold red")
    text.append(L(f"context tokens: {n} / {limit}\n",
                 f"токенов контекста: {n} / {limit}\n"))
    text.append(L(f"window {context.window} ({source}) · model {ctx.config.model} · "
                 f"provider {ctx.config.provider}\n",
                 f"окно {context.window} ({source}) · модель {ctx.config.model} · "
                 f"провайдер {ctx.config.provider}\n"), style="dim")
    text.append(L(f"messages: {len(msgs)}",
                 f"сообщений: {len(msgs)}"), style="dim")
    if context.trimmed:
        text.append(L(f" · {context.trimmed} folded into the digest",
                     f" · {context.trimmed} свёрнуто в конспект"), style="dim")
    text.append(L(f" · tools: {tools_n} · skills: {skills_n}",
                 f" · инструментов: {tools_n} · скилов: {skills_n}") + "\n", style="dim")
    if plan_line:
        text.append(plan_line + "\n", style="dim")
    if pct >= 80:
        text.append(L("nearly full — /compact folds the oldest turns, /reset starts over",
                     "почти полно — /compact свернёт старые реплики, /reset начнёт заново"),
                    style="bold yellow")
    else:
        text.append(L("free it: /compact · measure the window: /window measure",
                     "освободить: /compact · померить окно: /window measure"), style="dim")
    return CommandResult(output=text)
