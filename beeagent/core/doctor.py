"""Builtin `/doctor` — the install check-up that is always there.

The REPL, the TUI and the update flow all name `/doctor` when something goes
wrong ("`/doctor` says why"), but the full check-up lived in an installable
plugin template — a fresh install that needs it most answers "unknown
command". So the essentials live here, in the core, with no network and no
third-party imports: which Python, whether the console speaks UTF-8, whether
`beeagent.json` parses, whether the model's window is measured or guessed,
what the cache and sessions look like, and whether any extension failed to
load. The plugin template keeps the longer version (Termux specifics, MCP
pending list); this one is the subset support asks for first.
"""
from __future__ import annotations

COMMAND_NAME = "doctor"
USAGE = "/doctor"
DESCRIPTION = "Check the install, the model and the loaded extensions"

OK, WARN, BAD = "✅", "⚠️", "❌"


def register_command() -> bool:
    """Put `/doctor` into the shared command machinery, the way `/trust` does.

    The category is deliberately `"plugins"`: the shipped `doctor` plugin
    template registers this same name with the full check-up (Termux rows and
    all), and `ExtensionAPI.command` only replaces commands in the `plugins`
    category — a core category here would refuse the plugin with "name taken"
    and the install would lose its own check-up. So this core version is the
    fallback that answers on a fresh install (the REPL, the TUI and the update
    flow all name `/doctor`), and installing the catalog plugin upgrades it in
    place. Both interfaces go through `commands.dispatch`, covering the classic
    REPL, the Textual TUI and one-shot runs. Idempotent.
    """
    from beeagent.ui import commands as core

    existing = next((c for c in core.COMMANDS if c.name == COMMAND_NAME), None)
    if existing is not None and core.HANDLERS.get(COMMAND_NAME) is _cmd_doctor:
        return True
    if existing is None:
        core.add_command(COMMAND_NAME, DESCRIPTION, usage=USAGE, category="plugins")
    core.HANDLERS[COMMAND_NAME] = _cmd_doctor
    return True


def _version(name: str) -> tuple[str, str]:
    try:
        __import__(name)
    except Exception as e:
        return "", f"{name}: {e}"
    try:
        from importlib.metadata import version

        return version(name), ""
    except Exception:
        return "installed", ""


def _checks(ctx) -> list[tuple[str, str, str, str]]:
    import json
    import os
    import platform
    import shutil
    import sys
    from pathlib import Path

    rows: list[tuple[str, str, str, str]] = []
    agent = getattr(ctx, "agent", None)
    config = getattr(ctx, "config", None)
    workdir = Path(getattr(agent, "workdir", ".") or ".")

    py = sys.version_info
    rows.append((OK if py >= (3, 10) else BAD, "python",
                 f"{py.major}.{py.minor}.{py.micro} · {sys.executable}",
                 "" if py >= (3, 10) else "BeeCode needs 3.10 or newer"))

    rows.append((OK, "system", f"{platform.system()} {platform.release()} · {platform.machine()}",
                 ""))

    which = shutil.which("beecode")
    rows.append((OK if which else WARN, "`beecode` on PATH", which or "not found",
                 "" if which else "npm install -g beecode, or run python -m beeagent.cli"))

    git = shutil.which("git")
    rows.append((OK if git else WARN, "git", git or "not found",
                 "" if git else "install git — /commit and the diff tools need it"))

    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    rows.append((OK if "utf" in encoding else WARN, "console encoding",
                 encoding or "unknown",
                 "" if "utf" in encoding else "chcp 65001, or set PYTHONIOENCODING=utf-8"))

    for package, hint in (("g4f", "pip install -U g4f"),
                          ("tiktoken", "pip install tiktoken (counts fall back without it)"),
                          ("rich", "pip install -U rich")):
        value, error = _version(package)
        rows.append((OK if not error else (WARN if package == "tiktoken" else BAD),
                     package, value or error, "" if not error else hint))

    config_path = workdir / "beeagent.json"
    if config_path.exists():
        try:
            body = json.loads(config_path.read_text(encoding="utf-8"))
            rows.append((OK, "beeagent.json", f"{len(body)} keys", ""))
        except (OSError, ValueError) as e:
            rows.append((BAD, "beeagent.json", str(e)[:60],
                         "fix the JSON or delete the file — BeeCode runs on defaults"))
    else:
        rows.append((WARN, "beeagent.json", "not present", "created on the first /config change"))

    model = getattr(config, "model", "") if config else ""
    provider = getattr(config, "provider", "") if config else ""
    try:
        from beeagent.core import windows
        from beeagent.core.context import advertised_window

        measured = bool(model) and bool(windows.measured(model))
        size = advertised_window(model) if model else 0
        rows.append((OK if measured else WARN, "model window",
                     f"{model or '?'} · {size} tokens · {'measured' if measured else 'claimed'}",
                     "" if measured else f"/window measure {model}"))
    except Exception:
        rows.append((WARN, "model window", model or "?", ""))
    rows.append((OK if provider else BAD, "provider", provider or "none",
                 "" if provider else "/providers lists them"))

    cache_dir = workdir / ".beeagent" / "cache"
    files = list(cache_dir.glob("*.json")) if cache_dir.is_dir() else []
    size_kb = sum(f.stat().st_size for f in files) // 1024 if files else 0
    rows.append((OK if len(files) < 5000 else WARN, "economy cache",
                 f"{len(files)} answers · {size_kb} KB",
                 "delete .beeagent/cache — answers are only reused for the same prompt"
                 if len(files) >= 5000 else ""))

    sessions_dir = workdir / ".beeagent" / "sessions"
    sessions = list(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    rows.append((OK if sessions else WARN, "saved sessions", f"{len(sessions)}",
                 "" if sessions else "/save writes the first one"))

    plugins = getattr(agent, "plugins", None) if agent is not None else None
    if plugins is not None:
        errors = list(getattr(plugins, "load_errors", []))
        installed = len(getattr(plugins, "tool_names", []))
        rows.append((OK if not errors else BAD, "extensions",
                     f"{installed} tools loaded" + (f" · {len(errors)} errors" if errors else ""),
                     "; ".join(errors)[:120]))
    else:
        rows.append((WARN, "extensions", "no agent attached", ""))

    trusted = os.environ.get("BEECODE_TRUSTED_DIRS", "")
    rows.append((OK, "trusted dirs",
                 trusted or "none permanent (BEECODE_TRUSTED_DIRS is empty)",
                 "/dirs add <path> grants one for this session"))

    rows.append((OK if os.access(workdir, os.W_OK) else BAD, "project writable",
                 str(workdir), "" if os.access(workdir, os.W_OK) else "choose a folder you own"))
    return rows


def _cmd_doctor(ctx, args):
    from rich.table import Table

    from beeagent.ui import skin
    from beeagent.ui.commands import CommandResult

    table = Table(title="🐝 BeeCode check-up", show_header=True,
                  header_style="bold #ffcc00", **skin.frame_kwargs("#ffcc00"))
    table.add_column("", width=2)
    table.add_column("check", style="bold #ffcc00")
    table.add_column("found")
    table.add_column("if it is wrong", style="dim")
    for status, name, value, fix in _checks(ctx):
        table.add_row(status, name, str(value)[:90], fix[:120])
    return CommandResult(output=table)
