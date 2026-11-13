"""`/bug` — pack a crash into a file the user can send.

A traceback in a terminal is where bug reports go to die: nobody copies it,
and when they do the keys come along. This writes one Markdown file under
`<workdir>/.beeagent/` with the version, the platform, a sanitized config
(names and counts, never secret values), and the tail of the session — and
tells the user to read it before sending, because the transcript is theirs
and may hold what they pasted into it.
"""
from __future__ import annotations

import platform
import sys
import time
from pathlib import Path

from beeagent.i18n import L
from beeagent.tools.base import write_text_preserving

COMMAND_NAME = "bug"
USAGE = "/bug [what happened]"
DESCRIPTION = "Pack version, config and session tail into a report file"

MAX_MESSAGES = 50
MAX_CHARS = 120000


def _safe_config(config) -> dict:
    """Counts and names only. api_keys, pool_token and endpoint URLs stay out:
    a report that cannot carry a secret cannot leak one."""
    keys = getattr(config, "api_keys", None) or {}
    customs = getattr(config, "custom_providers", None) or []
    permissions = getattr(config, "permissions", None)
    return {
        "provider": str(getattr(config, "provider", "?")),
        "model": str(getattr(config, "model", "?")),
        "mode": str(getattr(config, "mode", "?")),
        "language": str(getattr(config, "language", "?")),
        "skin": str(getattr(config, "skin", "") or ""),
        "own_keys_stored": len(keys),
        "custom_providers": len(customs),
        "permissions_mode": str(getattr(permissions, "mode", "?")
                                if permissions is not None else "?"),
    }


def _render(config, session, note: str) -> str:
    from beeagent import __version__

    lines = [f"# BeeCode bug report ({__version__})", "",
             f"date: {time.strftime('%Y-%m-%d %H:%M:%S')}",
             f"python: {sys.version_info.major}.{sys.version_info.minor}."
             f"{sys.version_info.micro} · "
             f"{platform.system()} {platform.release()}",
             "", "## config (no secrets)"]
    for key, value in _safe_config(config).items():
        lines.append(f"- {key}: {value}")
    lines += ["", "## what happened", note or "(not described — /bug <text> says it)"]
    messages = list(getattr(session, "messages", None) or [])
    tail = messages[-MAX_MESSAGES:]
    lines += ["", f"## session tail ({len(tail)} of {len(messages)} messages)",
              f"session: {getattr(session, 'session_id', '?')}"]
    size = 0
    for message in tail:
        block = f"### {message.role}\n{(message.content or '').strip()}\n"
        if size + len(block) > MAX_CHARS:
            lines.append("… (cut: report cap reached)")
            break
        size += len(block)
        lines.append(block)
    return "\n".join(lines) + "\n"


def register_command() -> bool:
    """Put `/bug` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI and one-shot runs. Idempotent, core
    category so no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_bug:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="info")
    core.HANDLERS[COMMAND_NAME] = _cmd_bug
    return True


def _cmd_bug(ctx, args):
    from rich.text import Text

    from beeagent.ui.commands import CommandResult

    workdir = getattr(getattr(ctx, "agent", None), "workdir", None) or "."
    note = " ".join(args).strip()
    stamp = time.strftime("%Y%m%d_%H%M%S")
    target = Path(workdir) / ".beeagent" / f"bug-{stamp}.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_preserving(target, _render(ctx.config, ctx.session, note))
    except OSError as e:
        return CommandResult(output=Text(
            L(f"could not write the report: {e}", f"не смог записать отчёт: {e}"),
            style="bold red"))
    try:
        shown = target.relative_to(Path(workdir))
    except ValueError:
        shown = target
    return CommandResult(output=Text(L(
        f"report written: {shown} — read it before sending (the transcript is "
        f"yours and may hold what you pasted), then send it to "
        f"https://github.com/egorVasile/beecode/issues",
        f"отчёт записан: {shown} — прочитай перед отправкой (переписка твоя, "
        f"в ней может быть то, что ты вставлял), потом отправь на "
        f"https://github.com/egorVasile/beecode/issues"),
        style="bold green"))
