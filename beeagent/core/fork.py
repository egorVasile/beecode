"""`/fork` — clone this conversation into a new thread, keep the original.

Codex's `/fork` exists for the moment a turn could go two ways: try the risky
idea in the copy, and the original stays exactly where it was. The copy gets a
fresh session id (so `/sessions` shows both), the same messages, and is saved
to disk before the switch — a fork that loses the parent on a crash is not a
fork. The parent is saved too, so abandoning the copy costs nothing: `/continue
<parent-id>` walks straight back.
"""
from __future__ import annotations

COMMAND_NAME = "fork"
USAGE = "/fork"
DESCRIPTION = "Clone this conversation into a new thread (the original stays)"


def register_command() -> bool:
    """Put `/fork` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI and one-shot runs. Idempotent, core
    category so no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_fork:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="session")
    core.HANDLERS[COMMAND_NAME] = _cmd_fork
    return True


def _cmd_fork(ctx, args):
    from rich.text import Text

    from beeagent.core.session import Message, Session
    from beeagent.i18n import L
    from beeagent.ui.commands import CommandResult

    session = getattr(ctx, "session", None)
    if session is None or not getattr(session, "messages", None):
        return CommandResult(output=Text(L(
            "nothing to fork: this session holds no turns yet.",
            "форкать нечего: в этой сессии пока нет реплик."), style="dim"))
    workdir = getattr(getattr(ctx, "agent", None), "workdir", None) or "."
    try:
        session.save(workdir)
    except Exception:
        pass  # the parent on disk is a courtesy; the copy below is the promise
    child = Session()
    for m in session.messages:
        child.messages.append(Message(
            role=m.role, content=m.content,
            tool_calls=list(m.tool_calls or []),
            tool_result=m.tool_result))
    try:
        child.save(workdir)
    except OSError as e:
        return CommandResult(output=Text(
            L(f"could not save the fork: {e}", f"не смог сохранить форк: {e}"),
            style="bold red"))
    parent_id = session.session_id
    ctx.session = child
    return CommandResult(output=Text(L(
        f"forked {len(child.messages)} messages: {parent_id} → {child.session_id}. "
        f"The original is untouched — /continue {parent_id} walks back.",
        f"форкнуто сообщений: {len(child.messages)}: {parent_id} → {child.session_id}. "
        f"Оригинал не тронут — /continue {parent_id} вернёт назад."),
        style="bold green"))
