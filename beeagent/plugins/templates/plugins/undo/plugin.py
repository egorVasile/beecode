"""Undo for files the agent overwrote.

BeeCode edits through tools, so a pre-image can be taken at the moment of the
change: `tool_start` fires before the write lands. `/undo` puts the file back
the way it was and keeps what it replaced — undoing an undo should not be the
one thing that cannot be undone.
"""
import hashlib
import json
import shutil
import time
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024

# Things nobody wants a snapshot of: a repository keeps its own history, and a
# copied venv is gigabytes of files nobody edited by hand.
SKIP_DIRS = {".git", ".beeagent", "node_modules", "__pycache__", ".venv", "venv",
             "env", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build", ".idea"}

WRITING_TOOLS = {"write", "edit", "write_file", "edit_file", "create_file"}


def root(api) -> Path:
    agent = getattr(api, "agent", None)
    return Path(getattr(agent, "workdir", None) or ".")


def store(api) -> Path:
    return root(api) / ".beeagent" / "undo"


def resolve(api, raw: str) -> Path:
    path = Path(raw).expanduser()
    return path if path.is_absolute() else root(api) / path


def entry_dir(api, path: Path) -> Path:
    key = hashlib.sha1(str(path).lower().encode("utf-8")).hexdigest()[:12]
    return store(api) / key


def worth_copying(api, path: Path) -> bool:
    """Only files inside the project, small enough, and not another tool's output."""
    base = root(api)
    try:
        relative = path.resolve().relative_to(base.resolve())
    except (ValueError, OSError):
        return False
    if any(part in SKIP_DIRS for part in relative.parts):
        return False
    try:
        return path.is_file() and path.stat().st_size <= MAX_BYTES
    except OSError:
        return False


def save_copy(api, raw_path: str) -> Path:
    """Snapshot the bytes that are about to be replaced; "" when there is nothing."""
    path = resolve(api, raw_path)
    if not worth_copying(api, path):
        return None
    entry = entry_dir(api, path)
    entry.mkdir(parents=True, exist_ok=True)
    number = len(list(entry.glob("copy-*")))
    name = f"copy-{number:03d}"
    target = entry / name
    shutil.copy2(path, target)
    (entry / f"{name}.meta").write_text(json.dumps(
        {"path": str(path), "saved_at": time.time(), "size": path.stat().st_size},
        ensure_ascii=False), encoding="utf-8")
    return target


def copies(api) -> list[dict]:
    """Every saved pre-image, newest first."""
    found = []
    base = store(api)
    if not base.is_dir():
        return found
    for meta in base.glob("*/copy-*.meta"):
        try:
            body = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        body["entry"] = str(meta.parent)
        body["name"] = meta.name[:-len(".meta")]
        found.append(body)
    return sorted(found, key=lambda b: b.get("saved_at", 0), reverse=True)


def setup(api) -> None:
    def watch(event: str, data: dict) -> None:
        if event != "tool_start" or data.get("tool") not in WRITING_TOOLS:
            return
        args = data.get("args") or {}
        raw = args.get("path") or args.get("file") or args.get("file_path") or ""
        if raw:
            save_copy(api, str(raw))

    api.event("tool_start", watch)
    api.setting("auto", True, "save a copy of a file before BeeCode overwrites it")

    def undo(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        requested = " ".join(args).strip()
        if requested in ("list", "-l", "--list"):
            return CommandResult(output=_listing(api))
        if not store(api).is_dir():
            return CommandResult(output=Text(
                "nothing to undo yet — no file has been overwritten in this project",
                style="dim"))

        wanted = None
        if requested:
            entry = entry_dir(api, resolve(api, requested))
            saved = sorted(entry.glob("copy-*"))
            if saved:
                wanted = {"path": str(resolve(api, requested)), "entry": str(entry),
                          "name": saved[-1].name}
            else:
                return CommandResult(output=Text(
                    f"no saved copy of {requested} — /undo list shows what can be restored",
                    style="bold yellow"))
        else:
            saved = copies(api)
            wanted = saved[0] if saved else None
        if wanted is None:
            return CommandResult(output=Text("no saved copies yet", style="dim"))

        path = Path(wanted["path"])
        copy = Path(wanted["entry"]) / wanted["name"]
        kept = ""
        if path.is_file():
            kept = "discarded-" + wanted["name"]
            shutil.copy2(path, Path(wanted["entry"]) / kept)
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(copy, path)
        note = f"  what was there is kept as {kept}" if kept else "  the file did not exist"
        return CommandResult(output=Text.assemble(
            Text("🐝 restored ", style="bold #ffcc00"), Text(str(path)),
            Text(f"  from {wanted['name']}", style="dim"), Text(note, style="dim")))

    api.command("undo", "Put back a file BeeCode overwrote (/undo list)", undo,
                usage="/undo [path|list]")


def _listing(api):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title="🐝 saved copies", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column("when", style="dim")
    table.add_column("file")
    table.add_column("copy", style="dim")
    base = root(api)
    for body in copies(api)[:20]:
        stamp = body.get("saved_at", 0)
        when = time.strftime("%m-%d %H:%M", time.localtime(stamp)) if stamp else "?"
        path = body.get("path", "")
        try:
            shown = str(Path(path).relative_to(base))
        except (ValueError, OSError):
            shown = path
        table.add_row(when, shown, body.get("name", ""))
    return table
