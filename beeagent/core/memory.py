"""Project memory files: `/init` scaffolds one, `/memory` reads them back.

Claude Code writes `CLAUDE.md`, Codex writes `AGENTS.md` — BeeCode's own system
prompt already tells the model to read both (`core/context.py`), so whichever
name the project already uses is honoured. `/init` creates the one file that is
missing neither: `AGENTS.md`, the name both ecosystems have converged on, with
a scaffold the model can actually fill in (how to run, how to test, what not to
touch). An existing file is never overwritten — that would delete the one thing
memory is for. `/memory` lists what exists (project files plus the global
`~/.beecode/MEMORY.md`) and prints a head of each, so a stale note is visible
before it misleads a turn.
"""
from __future__ import annotations

from pathlib import Path

from beeagent.i18n import L
from beeagent.tools.base import write_text_preserving

INIT_NAME = "init"
INIT_USAGE = "/init"
INIT_DESCRIPTION = "Scaffold AGENTS.md project memory (never overwrites)"

MEMORY_NAME = "memory"
MEMORY_USAGE = "/memory [name]"
MEMORY_DESCRIPTION = "Show project and global memory files"

#: The files BeeCode's system prompt tells the model to read, in the order it
#: names them. The global one is ours alone: a place for "always do X" notes
#: that belong to the install, not to any checkout.
PROJECT_FILES = ("AGENTS.md", "CLAUDE.md")
GLOBAL_FILE = Path.home() / ".beecode" / "MEMORY.md"

HEAD_LINES = 60


def _scaffold(project: str) -> str:
    return f"""# {project} — agent notes

Instructions for AI coding agents working in this repo. Keep it short:
what the model cannot derive from the code itself.

## How to run

- Install: (fill in — e.g. `pip install -e .` / `npm install`)
- Tests: (fill in — e.g. `pytest -q` / `npm test`)
- Lint: (fill in, or delete this line)

## Conventions

- (naming, error handling, commit style — one line each)
- (what NOT to do — e.g. "never commit without asking")

## Layout

- (where the code lives, in three lines or fewer)
"""


def _workdir(ctx) -> str:
    return getattr(getattr(ctx, "agent", None), "workdir", None) or "."


def _project_files(workdir: str) -> list[Path]:
    root = Path(workdir)
    return [root / name for name in PROJECT_FILES]


def register_command() -> bool:
    """Put `/init` and `/memory` into the shared command machinery.

    Same trick as `core/trust.py`: both interfaces go through
    `commands.dispatch`, so one registration covers the classic REPL, the
    Textual sidebar and one-shot runs. Idempotent, core categories so no
    plugin can take the names back out.
    """
    from beeagent.ui import commands as core

    for name, description, usage, handler, category in (
        (INIT_NAME, INIT_DESCRIPTION, INIT_USAGE, _cmd_init, "session"),
        (MEMORY_NAME, MEMORY_DESCRIPTION, MEMORY_USAGE, _cmd_memory, "session"),
    ):
        existing = next((c for c in core.COMMANDS if c.name == name), None)
        if existing is not None and core.HANDLERS.get(name) is handler:
            continue
        if existing is None:
            core.add_command(name, description, usage=usage, category=category)
        core.HANDLERS[name] = handler
    return True


def _cmd_init(ctx, args):
    """Create `AGENTS.md` once; refuse when memory already exists."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    workdir = _workdir(ctx)
    present = [p for p in _project_files(workdir) if p.is_file()]
    if present:
        names = ", ".join(p.name for p in present)
        return CommandResult(output=Text(L(
            f"memory already exists: {names} — /init never overwrites. "
            f"Read it with /memory, edit it with /read + your editor.",
            f"память уже есть: {names} — /init никогда не перезаписывает. "
            f"Прочитай через /memory, правь через /read и редактор."),
            style="bold yellow"))
    target = Path(workdir) / "AGENTS.md"
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write_text_preserving(target, _scaffold(Path(workdir).resolve().name))
    except OSError as e:
        return CommandResult(output=Text(
            L(f"could not write AGENTS.md: {e}", f"не смог записать AGENTS.md: {e}"),
            style="bold red"))
    return CommandResult(output=Text(L(
        "wrote AGENTS.md — fill in the run/test lines so the next session "
        "starts with them. The model reads this file on its own.",
        "записал AGENTS.md — впиши строки запуска/тестов, и следующая сессия "
        "начнётся с них. Модель читает этот файл сама."),
        style="bold green"))


def _head(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return []
    if len(lines) > HEAD_LINES:
        return lines[:HEAD_LINES] + [f"… ({len(lines) - HEAD_LINES} more lines)"]
    return lines


def _cmd_memory(ctx, args):
    """List memory files, or print one of them (first 60 lines)."""
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    workdir = _workdir(ctx)
    files = _project_files(workdir) + [GLOBAL_FILE]
    if args:
        want = " ".join(args).strip().lower()
        for path in files:
            if path.name.lower() == want or path.stem.lower() == want:
                if not path.is_file():
                    return CommandResult(output=Text(L(
                        f"{path.name} does not exist yet — /init scaffolds "
                        f"AGENTS.md for this project.",
                        f"{path.name} пока нет — /init создаст AGENTS.md для проекта."),
                        style="dim"))
                body = "\n".join(_head(path)) or L("(empty)", "(пусто)")
                return CommandResult(output=Text(f"─── {path} ───\n{body}"))
        return CommandResult(output=Text(L(
            f"no memory file called “{args[0]}” — project: "
            f"{', '.join(PROJECT_FILES)}; global: ~/.beecode/MEMORY.md",
            f"файла памяти «{args[0]}» нет — проект: "
            f"{', '.join(PROJECT_FILES)}; глобальный: ~/.beecode/MEMORY.md"),
            style="bold red"))
    text = Text()
    for path in files:
        if path.is_file():
            size = path.stat().st_size
            text.append(f"✔ {path}", style="bold #ffcc00")
            text.append(f"  {size} bytes\n", style="dim")
            for line in _head(path)[:12]:
                text.append(f"  {line[:100]}\n", style="dim")
        else:
            text.append(f"· {path}", style="dim")
            text.append(L("  (missing)\n", "  (нет)\n"), style="dim")
    text.append(L("full file: /memory <name> · scaffold: /init",
                 "целиком: /memory <имя> · создать: /init"), style="dim")
    return CommandResult(output=text)
