"""`/doctor` — one local look at everything that makes BeeCode work.

Every check here is a question support ends up asking by hand: which Python is
running, is the console able to print a bee, does the model it picked hold a
long conversation, did a plugin fail to load and get swallowed. Nothing in
this file touches the network — a self-check that can hang is worse than none.
"""
import importlib
import json
import os
import platform
import shutil
import sys
from pathlib import Path

OK = "✅"
WARN = "⚠️"
BAD = "❌"


def _version(name: str) -> str:
    try:
        importlib.import_module(name)
    except Exception as e:
        return "", f"{name}: {e}"
    try:
        from importlib.metadata import version

        return version(name), ""
    except Exception:
        return getattr(importlib.import_module(name), "__version__", "installed"), ""


def setup(api) -> None:
    def doctor(ctx, args):
        from beeagent.ui.commands import CommandResult

        rows = _checks(api)
        return CommandResult(output=_table(rows))

    api.command("doctor", "Check the install, the model and the loaded extensions",
                doctor, usage="/doctor")


def _checks(api) -> list[tuple[str, str, str, str]]:
    """(status, what, value, what to do) — empty "what to do" means nothing is wrong."""
    rows: list[tuple[str, str, str, str]] = []
    agent = getattr(api, "agent", None)
    config = getattr(api, "config", None) or getattr(agent, "config", None)
    workdir = Path(getattr(agent, "workdir", ".") or ".")

    py = sys.version_info
    rows.append((OK if py >= (3, 10) else BAD, "python",
                 f"{py.major}.{py.minor}.{py.micro} · {sys.executable}",
                 "" if py >= (3, 10) else "BeeCode needs 3.10 or newer"))

    rows.append((OK, "system", f"{platform.system()} {platform.release()} · {platform.machine()}",
                 ""))

    prefix = os.environ.get("PREFIX", "")
    # Android is not a Linux distribution: there is no apt, no /usr, and pip
    # cannot compile what is written in Rust or C. Everything BeeCode cannot get
    # there has a `pkg install` line, so say which one.
    on_termux = "com.termux" in prefix or "com.termux" in os.environ.get("HOME", "")
    if on_termux:
        rows.append((OK, "termux prefix", prefix or os.environ.get("HOME", ""), ""))
        bash = shutil.which("bash")
        rows.append((OK if bash else WARN, "bash for the shell tool", bash or "not found",
                     "" if bash else "pkg install bash"))
        storage = Path.home() / "storage"
        rows.append((OK if storage.is_dir() else WARN, "shared storage",
                     "mounted" if storage.is_dir() else "not mounted",
                     "" if storage.is_dir()
                     else "termux-setup-storage — without it BeeCode sees only its own home"))

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

    for package, hint in (("g4f", "pip install -U g4f"), ("tiktoken", "pip install tiktoken"),
                          ("rich", "pip install -U rich"), ("prompt_toolkit", "pip install -U prompt_toolkit")):
        value, error = _version(package)
        if error and on_termux and package == "g4f":
            # g4f is pure Python; what a phone cannot build are two packages it
            # declares that BeeCode never imports. --no-deps skips them.
            rows.append((WARN, package, "not installed",
                         "g4f itself needs no compiler — its declared pycryptodome/brotli do. "
                         "See the Termux section of the README: --no-deps, plus "
                         "--no-binary=aiohttp with AIOHTTP_NO_EXTENSIONS=1"))
            continue
        if error and on_termux and package == "tiktoken":
            rows.append((WARN, package, "not installed",
                         "normal on Android: it is a Rust extension. Counts fall back to an "
                         "over-estimate, which trims early instead of overrunning the window"))
            continue
        rows.append((OK if not error else BAD, package, value or error, "" if not error else hint))

    config_path = workdir / "beeagent.json"
    if config_path.exists():
        try:
            body = json.loads(config_path.read_text(encoding="utf-8"))
            rows.append((OK, "beeagent.json", f"{len(body)} keys", ""))
        except (OSError, ValueError) as e:
            rows.append((BAD, "beeagent.json", str(e)[:60],
                         "fix the JSON or delete the file — BeeCode runs on defaults"))
    else:
        rows.append((WARN, "beeagent.json", "not present", "created the first time you run /config"))

    model = getattr(config, "model", "") if config else ""
    provider = getattr(config, "provider", "") if config else ""
    from beeagent.core import windows
    from beeagent.core.context import advertised_window

    measured = bool(model) and bool(windows.measured(model))
    size = advertised_window(model) if model else 0
    rows.append(((OK if measured else WARN), "model window",
                 f"{model or '?'} · {size} tokens · {'measured' if measured else 'claimed'}",
                 "" if measured else f"/window measure {model} — the number is a guess until then"))

    rows.append((OK if provider else BAD, "provider", provider or "none",
                 "" if provider else "/provider g4f"))

    cache_dir = workdir / ".beeagent" / "cache"
    files = list(cache_dir.glob("*.json")) if cache_dir.is_dir() else []
    size_kb = sum(f.stat().st_size for f in files) // 1024 if files else 0
    rows.append((OK if len(files) < 5000 else WARN, "economy cache",
                 f"{len(files)} answers · {size_kb} KB",
                 "delete .beeagent/cache — old answers are only reused for the same prompt"
                 if len(files) >= 5000 else ""))

    sessions_dir = workdir / ".beeagent" / "sessions"
    sessions = list(sessions_dir.glob("*.json")) if sessions_dir.is_dir() else []
    rows.append((OK if sessions else WARN, "saved sessions", f"{len(sessions)}",
                 "" if sessions else "/save writes the first one"))

    plugins = getattr(agent, "plugins", None)
    if plugins is not None:
        errors = list(getattr(plugins, "load_errors", []))
        installed = len(getattr(plugins, "tool_names", []))
        rows.append((OK if not errors else BAD, "extensions",
                     f"{installed} tools loaded" + (f" · {len(errors)} errors" if errors else ""),
                     "; ".join(errors)[:120]))
        servers = getattr(plugins, "pending_mcp", [])
        if servers:
            rows.append((WARN, "mcp not connected", ", ".join(str(s) for s in servers)[:60],
                         "/mcp connect <name> — it is slow, so it never runs at startup"))
    else:
        rows.append((WARN, "extensions", "no agent attached", ""))

    writable = os.access(workdir, os.W_OK)
    rows.append((OK if writable else BAD, "project writable", str(workdir),
                 "" if writable else "choose a folder you own"))

    return rows


def _table(rows):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title="🐝 BeeCode check-up", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column("", width=2)
    table.add_column("check", style="bold #ffcc00")
    table.add_column("found")
    table.add_column("if it is wrong", style="dim")
    for status, name, value, fix in rows:
        table.add_row(status, name, str(value)[:90], fix[:120])
    return table
