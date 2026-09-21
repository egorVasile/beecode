"""What this session changed, without asking the model to remember.

An agent that says "I updated three files" is asking you to trust it. BeeCode
knows which files it wrote — the tool calls pass through the same events a
plugin already sees — so `/changes` lists them with their git state. BeeCode's
own `/diff <path>` then shows the real thing.
"""
import subprocess
import time
from pathlib import Path

WRITING_TOOLS = {"write", "edit", "write_file", "edit_file", "create_file"}


def _git(workdir: Path, *args: str) -> str:
    try:
        done = subprocess.run(["git", *args], cwd=str(workdir), capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout if done.returncode == 0 else ""


def _relative(workdir: Path, raw: str) -> str:
    path = Path(raw).expanduser()
    try:
        return str((path if path.is_absolute() else workdir / path).resolve()
                   .relative_to(workdir.resolve()))
    except (ValueError, OSError):
        return str(raw)


def setup(api) -> None:
    touched: dict[str, dict] = {}

    def watch(event: str, data: dict) -> None:
        if event != "tool_end" or data.get("tool") not in WRITING_TOOLS:
            return
        args = data.get("args") or {}
        raw = args.get("path") or args.get("file") or args.get("file_path") or ""
        if not raw:
            return
        touched[_display(api, str(raw))] = {
            "at": time.time(), "error": bool(data.get("error")), "tool": data.get("tool"),
        }

    api.event("tool_end", watch)

    def changes(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        workdir = _root(api)
        wrote = _git(workdir, "status", "--porcelain")
        state = {}
        for line in wrote.splitlines():
            if len(line) > 3:
                state[line[3:].strip()] = line[:2].strip()
        if not touched:
            if not state:
                return CommandResult(output=Text(
                    "this session has not written a file, and git sees no changes",
                    style="dim"))
            return CommandResult(output=_table(api, _from_git(workdir, state)))
        rows = []
        for name, info in sorted(touched.items(), key=lambda kv: kv[1]["at"], reverse=True):
            when = time.strftime("%H:%M:%S", time.localtime(info["at"]))
            rows.append((when, name, info["tool"],
                         "failed" if info["error"] else state.get(name, "written")))
        return CommandResult(output=_table(api, rows))

    api.command("changes", "Files this session wrote, and what git says about them",
                changes, usage="/changes")


def _root(api) -> Path:
    agent = getattr(api, "agent", None)
    return Path(getattr(agent, "workdir", ".") or ".")


def _display(api, raw: str) -> str:
    return _relative(_root(api), raw)


def _from_git(workdir: Path, state: dict) -> list[tuple]:
    now = time.strftime("%H:%M:%S")
    return [(now, name, "outside this session", code) for name, code in sorted(state.items())[:40]]


def _table(api, rows):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title="🐝 what changed", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column("when", style="dim")
    table.add_column("file")
    table.add_column("by", style="dim")
    table.add_column("state", style="bold #ffcc00")
    for row in rows:
        table.add_row(*[str(cell)[:80] for cell in row])
    return table
