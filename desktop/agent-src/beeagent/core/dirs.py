"""`/dirs` — see and grant the folders outside the working directory.

When the model reaches outside the project, the executor asks: decline, once,
or always (`core/executor.py`). That covers the moment — this command covers
everything around it: `/dirs` lists what this session already granted plus
what `BEECODE_TRUSTED_DIRS` grants permanently, `/dirs add <path>` grants a
folder up front (so the long task never stops to ask), `/dirs clear` takes the
session grants back. The Codex equivalent is `/sandbox-add-read-dir`; ours
grants the one policy BeeCode has (`tools/_path_policy.py`), read and write
together, because a second read-only policy would be a second thing to get
wrong.
"""
from __future__ import annotations

COMMAND_NAME = "dirs"
USAGE = "/dirs [add <path> | clear]"
DESCRIPTION = "List or grant folders outside the working directory"


def register_command() -> bool:
    """Put `/dirs` into the shared command machinery, the way `/trust` does.

    Both interfaces go through `commands.dispatch`, so one registration covers
    the classic REPL, the Textual TUI and one-shot runs. Idempotent, core
    category so no plugin can take the name back out.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_dirs:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="engine")
    core.HANDLERS[COMMAND_NAME] = _cmd_dirs
    return True


def _listing() -> str:
    import os

    from beeagent.i18n import L
    from beeagent.tools import _path_policy as policy

    lines = []
    granted = list(getattr(policy, "_granted", []))
    if policy.always():
        lines.append(L("this session: ALWAYS — outside folders run without asking",
                      "эта сессия: ВСЕГДА — внешние папки идут без спроса"))
    elif granted:
        lines.append(L("this session:", "эта сессия:"))
        lines.extend(f"  ✔ {g}" for g in granted)
    else:
        lines.append(L("this session: nothing granted yet",
                      "эта сессия: пока ничего не разрешено"))
    env = os.environ.get(policy.TRUSTED_DIRS_ENV, "")
    if env:
        lines.append(L(f"permanent ({policy.TRUSTED_DIRS_ENV}):", f"навсегда ({policy.TRUSTED_DIRS_ENV}):"))
        lines.extend(f"  ✔ {p}" for p in env.split(os.pathsep) if p.strip())
    else:
        lines.append(L(f"permanent: none — {policy.TRUSTED_DIRS_ENV} is empty",
                      f"навсегда: ничего — {policy.TRUSTED_DIRS_ENV} пуст"))
    return "\n".join(lines)


def _cmd_dirs(ctx, args):
    from rich.text import Text

    from beeagent.i18n import L
    from beeagent.tools import _path_policy as policy
    from beeagent.ui.commands import CommandResult

    word = (args[0].lower() if args else "")
    if word in ("", "list", "show", "ls"):
        return CommandResult(output=Text(
            _listing() + "\n" + L("grant one: /dirs add <path> · take back: /dirs clear",
                                 "разрешить: /dirs add <путь> · забрать: /dirs clear"),
            style="dim"))
    if word in ("clear", "reset", "revoke", "forget"):
        policy.forget_granted_roots()
        policy._always = False
        return CommandResult(output=Text(L(
            "session grants cleared — outside folders ask again. "
            f"({policy.TRUSTED_DIRS_ENV} still applies: it is permanent.)",
            "разрешения сессии убраны — внешние папки снова спросят. "
            f"({policy.TRUSTED_DIRS_ENV} остался: он навсегда.)"), style="dim"))
    if word in ("add", "grant", "allow", "+"):
        if len(args) < 2:
            return CommandResult(output=Text(L(
                "usage: /dirs add <path>", "использование: /dirs add <путь>"),
                style="bold red"))
        import os
        from pathlib import Path

        raw = " ".join(args[1:]).strip()
        candidate = Path(os.path.realpath(os.path.abspath(os.path.expanduser(raw))))
        if not str(raw).strip():
            return CommandResult(output=Text(L(
                "name a folder: /dirs add <path>", "назови папку: /dirs add <путь>"),
                style="bold red"))
        policy.grant_root(str(candidate))
        return CommandResult(output=Text(L(
            f"granted for this session: {candidate}",
            f"разрешено на эту сессию: {candidate}"), style="bold green"))
    return CommandResult(output=Text(
        L(f"usage: {USAGE}", f"использование: {USAGE}"), style="bold red"))
