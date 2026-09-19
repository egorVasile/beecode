"""Slash-command registry, completion logic, and dispatch.

Handlers return a `CommandResult` carrying a rich renderable (or plain string)
plus an optional UI action, so the same commands work in the classic REPL and
in the full-screen Textual TUI. Nothing here writes to the console directly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich import box

from beeagent.core.session import Session
from beeagent.i18n import L
from beeagent.plugins.catalog import TYPE_ICON
from beeagent.ui.components import BORDER, HONEY, bee_title

AVAILABLE_MODES = ["normal", "economy"]

THEMES = [
    "textual-dark", "textual-light", "nord", "gruvbox",
    "catppuccin-mocha", "catppuccin-latte", "dracula",
    "monokai", "solarized-light", "flexoki",
]

PROVIDER_META = {
    "g4f": ("free", "GPT4Free — no account needed"),
    "openai_compat": ("api", "OpenAI-compatible endpoint"),
    "ollama": ("local", "Local models via Ollama"),
}


@dataclass
class Command:
    name: str
    description: str
    arg: Optional[str] = None  # "model" | "provider" | "mode" | "session" | "theme" | "path" | None
    usage: Optional[str] = None
    category: str = "general"


COMMANDS: list[Command] = [
    # help / info
    Command("help", "Show all available commands", category="info"),
    Command("about", "About BeeCode", category="info"),
    Command("config", "Show current configuration", category="info"),
    Command("tools", "List registered tools", category="info"),
    Command("stats", "Show economy/request stats", category="info"),
    Command("token", "Show current context token usage", category="info"),
    Command("thinking", "Show the last model reasoning (scrollable)", category="info"),
    # model / provider / mode
    Command("model", "Switch the active model", arg="model", usage="/model <name>", category="engine"),
    Command("models", "List available models", category="engine"),
    Command("provider", "Switch the active provider", arg="provider", usage="/provider <name>", category="engine"),
    Command("providers", "List available providers", category="engine"),
    Command("mode", "Switch between normal and economy", arg="mode", usage="/mode <normal|economy>", category="engine"),
    Command("lang", "Switch the interface language", usage="/lang <en|ru>", category="engine"),
    # direct tool commands
    Command("run", "Run a shell command", usage="/run <command>", category="tools"),
    Command("read", "Read a file", arg="path", usage="/read <path>", category="tools"),
    Command("search", "Grep files by regex", usage="/search <pattern> [path]", category="tools"),
    Command("find", "Find files by glob", usage="/find <glob> [path]", category="tools"),
    Command("status", "git status", category="git"),
    Command("diff", "git diff", category="git"),
    Command("log", "git log", usage="/log [n]", category="git"),
    # session
    Command("history", "Open the full history (scrollable)", usage="/history [list]", category="session"),
    Command("session", "Show current session info", category="session"),
    Command("sessions", "List saved sessions", category="session"),
    Command("save", "Save the current session now", category="session"),
    Command("export", "Export session to a Markdown file", arg="path", usage="/export [path]", category="session"),
    Command("continue", "Load a saved session", arg="session", usage="/continue <id>", category="session"),
    Command("load", "Alias for /continue", arg="session", usage="/load <id>", category="session"),
    Command("reset", "Start a new session (clear history)", category="session"),
    Command("new", "Alias for /reset", category="session"),
    # extensions (skills / plugins / MCP servers)
    Command("plugins", "Browse the installable catalog", arg="plugin", usage="/plugins [filter]", category="extensions"),
    Command("plugin", "Install/remove/enable an extension", usage="/plugin <install|remove|list|enable|disable> [name]", category="extensions"),
    Command("skills", "List installed skills", category="extensions"),
    Command("skill", "Show a skill's instructions", arg="skill", usage="/skill <name>", category="extensions"),
    Command("mcp", "Manage MCP servers", usage="/mcp <list|add|remove|connect|tools>", category="extensions"),
    # ui
    Command("theme", "Switch color theme", arg="theme", usage="/theme <name>", category="ui"),
    Command("bee", "Toggle the animated bee", category="ui"),
    Command("clear", "Clear the screen / log", category="ui"),
    Command("quit", "Exit BeeCode", category="ui"),
]


@dataclass
class Suggestion:
    text: str
    start_position: int
    display: str
    meta: str


@dataclass
class CommandResult:
    output: object = None              # rich renderable | str | None
    action: Optional[str] = None       # "quit" | "clear" | None


@dataclass
class ReplContext:
    agent: object = None
    config: object = None
    session: object = None
    running: bool = True
    bee_enabled: bool = True
    theme: str = "textual-dark"


# --- dynamic completion sources ------------------------------------------

def available_models(ctx: ReplContext) -> list[str]:
    models: list[str] = []
    agent = ctx.agent
    if agent is not None:
        provider = agent.providers.get(getattr(ctx.config, "provider", ""))
        if provider is not None:
            # Full cross-provider catalog first (curated picks up front),
            # then anything else the provider itself declares.
            discover = getattr(provider, "discover_models", None)
            if callable(discover):
                models.extend(discover())
            for m in getattr(provider, "models", None) or []:
                if m not in models:
                    models.append(m)
    for cp in getattr(ctx.config, "custom_providers", []) or []:
        if getattr(cp, "model", None) and cp.model not in models:
            models.append(cp.model)
    if not models:
        from beeagent.providers.g4f_provider import G4fProvider
        models = G4fProvider.discover_models()
    return models


def available_providers(ctx: ReplContext) -> list[str]:
    names: list[str] = []
    agent = ctx.agent
    if agent is not None:
        names.extend(agent.providers.list_names())
    for builtin in ("g4f", "openai_compat", "ollama"):
        if builtin not in names:
            names.append(builtin)
    for cp in getattr(ctx.config, "custom_providers", []) or []:
        if cp.name not in names:
            names.append(cp.name)
    return names


def available_paths(ctx: ReplContext) -> list[str]:
    try:
        entries = sorted(p.name for p in Path(".").iterdir())
    except Exception:
        return []
    return entries[:300]


def catalog_names(ctx: ReplContext) -> list[str]:
    return [name for name, _ in catalog_choices(ctx)]


def installed_skill_names(ctx: ReplContext) -> list[str]:
    loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
    return [s.name for s in (loader.skills if loader is not None else [])]


# --- (value, label) pairs for the mouse pickers ---------------------------

def catalog_choices(ctx: ReplContext) -> list[tuple[str, str]]:
    manager, _ = _extensions(ctx)
    installed = manager.installed()
    return [
        (item.name,
         f"{TYPE_ICON.get(item.type, '•')} {item.name} — {item.description}"
         + ("  ✅" if item.name in installed else ""))
        for item in manager.catalog.items()
    ]


def skill_choices(ctx: ReplContext) -> list[tuple[str, str]]:
    loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
    skills = loader.skills if loader is not None else []
    return [(s.name, f"📚 {s.name} — {s.description}") for s in skills]


def mcp_choices(ctx: ReplContext) -> list[tuple[str, str]]:
    manager, _ = _extensions(ctx)
    out = []
    for name, cfg in manager.mcp_servers().items():
        command = f"{cfg.get('command', '')} {' '.join(cfg.get('args', []) or [])}".strip()
        out.append((name, f"🔌 {name} — {command}"))
    return out


def build_sources(ctx: ReplContext) -> dict[str, list[str]]:
    return {
        "model": available_models(ctx),
        "provider": available_providers(ctx),
        "mode": list(AVAILABLE_MODES),
        "session": Session.list_sessions(),
        "theme": list(THEMES),
        "path": available_paths(ctx),
        "plugin": catalog_names(ctx),
        "skill": installed_skill_names(ctx),
    }


def get_suggestions(text: str, sources: dict[str, list[str]]) -> list[Suggestion]:
    """Pure completion logic driven by the text before the cursor."""
    if not text.startswith("/"):
        return []

    parts = text.split(" ")

    if len(parts) == 1:
        word = parts[0]
        query = word[1:].lower()
        out: list[Suggestion] = []
        for cmd in COMMANDS:
            if cmd.name.startswith(query):
                out.append(Suggestion(
                    text="/" + cmd.name,
                    start_position=-len(word),
                    display="/" + cmd.name,
                    meta=cmd.description,
                ))
        return out

    cmd_name = parts[0][1:]
    cmd = next((c for c in COMMANDS if c.name == cmd_name), None)
    if cmd is None or not cmd.arg:
        return []

    word = parts[-1]
    options = sources.get(cmd.arg, [])
    out = []
    for opt in options:
        if opt.lower().startswith(word.lower()):
            out.append(Suggestion(
                text=opt,
                start_position=-len(word),
                display=opt,
                meta=cmd.arg,
            ))
    return out


# --- helpers --------------------------------------------------------------

def _err(msg: str) -> CommandResult:
    return CommandResult(output=Text(msg, style="bold red"))


def _ok(msg: str) -> CommandResult:
    return CommandResult(output=Text(msg, style="bold green"))


def _tool_panel(title: str, output: str, error: bool) -> Panel:
    return Panel(
        Text(output),
        title=bee_title(title),
        title_align="left",
        border_style="bold red" if error else BORDER,
        box=box.ROUNDED,
        padding=(0, 1),
    )


def _run_tool(ctx: ReplContext, tool_name: str, title: str, **kwargs) -> CommandResult:
    if ctx.agent is None:
        return _err("No agent available.")
    tool = ctx.agent.tools.get(tool_name)
    if tool is None:
        return _err(f"Tool '{tool_name}' is not registered.")
    try:
        res = tool.execute(**kwargs)
    except Exception as e:
        return _err(f"{tool_name} failed: {e}")
    return CommandResult(output=_tool_panel(title, res.output or "(no output)", res.error))


# --- handlers -------------------------------------------------------------

def _cmd_help(ctx, args):
    from beeagent.ui.components import commands_table
    return CommandResult(output=commands_table(COMMANDS))


def _cmd_about(ctx, args):
    tools_n = len(ctx.agent.tools.list_names()) if ctx.agent is not None else 0
    text = Text()
    text.append("BeeCode", style="bold #ffcc00")
    text.append(" — free AI coding agent powered by g4f\n", style="dim")
    text.append(f"version: 0.1.0\n")
    text.append(f"model:   ", style="dim"); text.append(f"{ctx.config.model}\n", style="bold #ffcc00")
    text.append(f"provider:", style="dim"); text.append(f"{ctx.config.provider}\n", style="bold #ffcc00")
    text.append(f"mode:    ", style="dim"); text.append(f"{ctx.config.mode}\n", style="bold #ffcc00")
    text.append(f"tools:   ", style="dim"); text.append(f"{tools_n}\n", style="bold #ffcc00")
    text.append(f"commands:", style="dim"); text.append(f"{len(COMMANDS)}\n", style="bold #ffcc00")
    return CommandResult(output=Panel(text, title=bee_title("About"), border_style=BORDER, box=box.ROUNDED))


def _cmd_models(ctx, args):
    from beeagent.ui.components import models_table
    return CommandResult(output=models_table(available_models(ctx)))


def _cmd_providers(ctx, args):
    from beeagent.ui.components import providers_table
    rows = []
    for name in available_providers(ctx):
        kind, desc = PROVIDER_META.get(name, ("custom", "Custom provider from config"))
        rows.append({"name": name, "type": kind, "desc": desc})
    return CommandResult(output=providers_table(rows))


def _cmd_model(ctx, args):
    if not args:
        return CommandResult(output=Text(f"current model: {ctx.config.model}  (see /models)", style="dim"))
    name = args[0]
    if name not in available_models(ctx):
        return _err(f"Unknown model '{name}'. Run /models to see the list.")
    ctx.config.model = name
    if ctx.agent is not None:
        ctx.agent.context.model = name
    return _ok(f"model → {name}")


def _cmd_provider(ctx, args):
    if not args:
        return CommandResult(output=Text(f"current provider: {ctx.config.provider}", style="dim"))
    name = args[0]
    if name not in available_providers(ctx):
        return _err(f"Unknown provider '{name}'. Run /providers to see the list.")
    ctx.config.provider = name
    return _ok(f"provider → {name}")


def _cmd_lang(ctx, args):
    from beeagent.i18n import LANGUAGES, get_lang, set_lang

    if not args:
        return CommandResult(output=Text(
            L(f"current language: {get_lang()}  (switch with /lang en or /lang ru)",
              f"текущий язык: {get_lang()}  (переключить /lang en или /lang ru)"), style="dim"))
    code = args[0].lower()
    if code not in LANGUAGES:
        return _err(L(f"unknown language '{code}'. Available: {', '.join(LANGUAGES)}",
                      f"неизвестный язык '{code}'. Доступны: {', '.join(LANGUAGES)}"))
    set_lang(code)
    if ctx.config is not None:
        ctx.config.language = code
        try:
            from beeagent.config.loader import save_config
            save_config(ctx.config, ctx.agent.workdir if ctx.agent is not None else ".")
        except OSError:
            pass
    return _ok(L(f"interface language → {code}", f"язык интерфейса → {code}"))


def _cmd_mode(ctx, args):
    if not args:
        return CommandResult(output=Text(f"current mode: {ctx.config.mode}", style="dim"))
    mode = args[0]
    if mode not in AVAILABLE_MODES:
        return _err(f"Unknown mode '{mode}'. Use normal or economy.")
    ctx.config.mode = mode
    if ctx.agent is not None:
        economy = ctx.agent.economy
        economy.mode = mode
        if mode == "economy" and economy.cache is None:
            from beeagent.utils.cache import ResponseCache
            economy.cache = ResponseCache(ctx.config.economy.cache_dir)
        elif mode == "normal":
            economy.cache = None
    return _ok(f"mode → {mode}")


def _cmd_tools(ctx, args):
    if ctx.agent is None:
        return _err("No agent available.")
    from beeagent.ui.components import tools_table
    return CommandResult(output=tools_table(ctx.agent.tools.list_tools()))


def _cmd_config(ctx, args):
    table = Table(title=bee_title("Configuration"), box=box.ROUNDED, border_style=BORDER, expand=False)
    table.add_column("Key", style="bold #ffcc00")
    table.add_column("Value")
    for k, v in ctx.config.model_dump().items():
        table.add_row(k, str(v))
    return CommandResult(output=table)


def _cmd_run(ctx, args):
    if not args:
        return _err("usage: /run <command>")
    return _run_tool(ctx, "bash", "bash", command=" ".join(args))


def _cmd_read(ctx, args):
    if not args:
        return _err("usage: /read <path>")
    return _run_tool(ctx, "read", f"read {args[0]}", path=args[0])


def _cmd_search(ctx, args):
    if not args:
        return _err("usage: /search <pattern> [path]")
    pattern = args[0]
    path = args[1] if len(args) > 1 else "."
    return _run_tool(ctx, "grep", f"grep {pattern}", pattern=pattern, path=path)


def _cmd_find(ctx, args):
    if not args:
        return _err("usage: /find <glob> [path]")
    pattern = args[0]
    path = args[1] if len(args) > 1 else "."
    return _run_tool(ctx, "glob", f"glob {pattern}", pattern=pattern, path=path)


def _cmd_status(ctx, args):
    return _run_tool(ctx, "git", "git status", command="status -s")


def _cmd_diff(ctx, args):
    return _run_tool(ctx, "git", "git diff", command="diff --stat")


def _cmd_log(ctx, args):
    n = args[0] if args and args[0].isdigit() else "10"
    return _run_tool(ctx, "git", "git log", command=f"log --oneline -{n}")


def _cmd_token(ctx, args):
    if ctx.agent is None:
        return _err("No agent available.")
    from beeagent.utils.tokens import count_tokens
    msgs = ctx.agent.context.build_messages(ctx.session.to_dicts(), ctx.agent.tools.to_schemas())
    n = count_tokens(json.dumps(msgs), ctx.config.model)
    limit = ctx.agent.context.max_tokens
    pct = int(100 * n / limit) if limit else 0
    text = Text()
    text.append(f"context tokens: ", style="dim")
    text.append(f"{n}", style="bold #ffcc00")
    text.append(f" / {limit}  ({pct}%)\n", style="dim")
    text.append(f"messages: {len(ctx.session.messages)}", style="dim")
    return CommandResult(output=text)


def _cmd_stats(ctx, args):
    if ctx.agent is None:
        return _err("No agent available.")
    s = ctx.agent.economy.get_stats()
    table = Table(title=bee_title("Stats"), box=box.ROUNDED, border_style=BORDER, expand=False)
    table.add_column("Metric", style="bold #ffcc00")
    table.add_column("Value")
    for k, v in s.items():
        table.add_row(str(k), str(v))
    return CommandResult(output=table)


def _cmd_thinking(ctx, args):
    # The viewer itself opens in the REPL (interactive only) via this action.
    return CommandResult(action="thinking_pager")


def history_body(session) -> str:
    """The whole conversation as plain text, for the scrollable viewer."""
    msgs = session.messages if session is not None else []
    blocks = []
    for i, m in enumerate(msgs, 1):
        content = (m.content or "").rstrip() or L("(empty)", "(пусто)")
        blocks.append(
            "\n".join([
                f"─── #{i} · {m.role} " + "─" * max(0, 46 - len(m.role)),
                content,
                "",
            ])
        )
    return "\n".join(blocks)


def _cmd_history(ctx, args):
    msgs = ctx.session.messages if ctx.session is not None else []
    if not msgs:
        return CommandResult(output=Text("(empty session)", style="dim"))
    if args and args[0].lower() in ("list", "кратко", "short"):
        text = Text()
        for i, m in enumerate(msgs, 1):
            snippet = (m.content or "").replace("\n", " ")[:80]
            text.append(f"{i:>3}. ", style="dim")
            text.append(f"[{m.role}]", style="bold #ffcc00")
            text.append(f" {snippet}\n")
        return CommandResult(output=text)
    return CommandResult(
        output=Text(L(f"  📜 history: {len(msgs)} messages — scroll with the wheel, q closes it",
                  f"  📜 история: {len(msgs)} сообщений — листай колесом, q закрывает"), style="dim"),
        action="history_pager",
    )


def _cmd_session(ctx, args):
    s = ctx.session
    count = len(s.messages) if s is not None else 0
    return CommandResult(output=Text(f"session: {s.session_id}   messages: {count}", style="dim"))


def _cmd_sessions(ctx, args):
    ids = Session.list_sessions()
    if not ids:
        return CommandResult(output=Text("no saved sessions", style="dim"))
    text = Text()
    for sid in ids:
        text.append(f"{sid}\n", style="bold #ffcc00")
    return CommandResult(output=text)


def _cmd_save(ctx, args):
    ctx.session.save()
    return _ok(f"saved session {ctx.session.session_id}")


def _cmd_export(ctx, args):
    path = args[0] if args else f".beeagent/exports/{ctx.session.session_id}.md"
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# BeeCode session {ctx.session.session_id}", ""]
    for m in ctx.session.messages:
        lines.append(f"## {m.role}")
        lines.append(m.content or "")
        lines.append("")
    p.write_text("\n".join(lines), encoding="utf-8")
    return _ok(f"exported to {p}")


def _cmd_continue(ctx, args):
    if not args:
        res = _cmd_sessions(ctx, args)
        return CommandResult(output=res.output, action=None)
    sid = args[0]
    try:
        ctx.session = Session.load(sid)
    except Exception as e:
        return _err(f"Could not load session '{sid}': {e}")
    return _ok(f"loaded session {sid}")


def _cmd_reset(ctx, args):
    ctx.session = Session()
    return _ok(f"new session {ctx.session.session_id}")


# --- extensions: skills, plugin packs, MCP servers -------------------------


def _extensions(ctx: ReplContext):
    """Return the live agent's (manager, loader), or a standalone manager."""
    loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
    if loader is not None:
        return loader.manager, loader
    from beeagent.plugins.manager import PluginManager
    return PluginManager(), None


def _reload_extensions(ctx: ReplContext) -> str:
    """Re-scan installed extensions after a change; describe what came in."""
    if ctx.agent is None or getattr(ctx.agent, "plugins", None) is None:
        return ""
    errors = ctx.agent.reload_extensions()
    loader = ctx.agent.plugins
    note = L(f"\n  🐝 loaded: {len(loader.skills)} skills, {len(loader.tool_names)} tools",
             f"\n  🐝 загружено: {len(loader.skills)} скилов, {len(loader.tool_names)} инструментов")
    for err in errors:
        note += f"\n  ⚠ {err}"
    return note


def _catalog_table(items, installed: dict, title: str) -> Table:
    table = Table(
        title=bee_title(title), header_style="bold " + HONEY,
        border_style=BORDER, box=box.SIMPLE_HEAVY,
    )
    table.add_column("", width=2)
    table.add_column("name", style="bold #ffcc00")
    table.add_column(L("description", "описание"))
    table.add_column(L("status", "статус"), style="green")
    for item in items:
        entry = installed.get(item.name)
        if entry is None:
            status = ""
        elif entry.get("enabled", True):
            status = L("✅ installed", "✅ установлен")
        else:
            status = L("⏸ disabled", "⏸ выключен")
        table.add_row(TYPE_ICON.get(item.type, "•"), item.name,
                      item.description, status)
    return table


def _cmd_plugins(ctx, args):
    """Browse (or filter) the installable catalog of skills/plugins/MCP."""
    manager, _ = _extensions(ctx)
    if args:
        query = " ".join(args)
        items = manager.catalog.items(query) or manager.catalog.search(query)
        title = L(f"Catalog: {query}", f"Каталог: {query}")
    else:
        items = manager.catalog.items()
        title = L("Catalog: skills, plugins and MCP servers", "Каталог: скилы, плагины и MCP-серверы")
    if not items:
        return _err(L("Nothing found. /plugins shows the whole catalog.", "Ничего не найдено. /plugins — показать весь каталог."))
    table = _catalog_table(items, manager.installed(), title)
    table.caption = Text(
        L("install: /plugin install <name> · pick with the mouse: /plugins",
                     "установить: /plugin install <name> · выбор мышкой: /plugins"), style="dim")
    return CommandResult(output=table)


def _cmd_plugin(ctx, args):
    if not args:
        return _cmd_plugins(ctx, [])
    sub, rest = args[0].lower(), args[1:]
    manager, _ = _extensions(ctx)
    installed = manager.installed()

    if sub == "list":
        if not installed:
            return CommandResult(output=Text(
                L("Nothing installed yet. /plugins opens the catalog.",
                "Ничего не установлено. /plugins — каталог."), style="dim"))
        from beeagent.plugins.catalog import CatalogItem
        items = [
            CatalogItem(name=name, type=entry.get("type", "plugin"),
                        category=entry.get("category", ""),
                        description=entry.get("description", ""),
                        source=entry.get("source", {}))
            for name, entry in installed.items()
        ]
        return CommandResult(output=_catalog_table(items, installed, L("Installed", "Установленное")))

    if not rest:
        return _err(f"usage: /plugin {sub} <name|git-url>")
    target = " ".join(rest)

    if sub == "install":
        try:
            report = manager.install(target)
        except Exception as e:
            return _err(L(f"could not install “{target}”: {e}", f"не удалось установить «{target}»: {e}"))
        installed = (f"✅ installed {TYPE_ICON.get(report['type'], '')} {report['name']} "
                     f"({report['type']})")
        native = (f"✅ установлен {TYPE_ICON.get(report['type'], '')} {report['name']} "
                  f"({report['type']})")
        return _ok(L(installed, native) + _reload_extensions(ctx))

    if sub in ("remove", "uninstall"):
        try:
            manager.uninstall(target)
        except Exception as e:
            return _err(L(f"could not remove “{target}”: {e}", f"не удалось удалить «{target}»: {e}"))
        tail = _reload_extensions(ctx)
        return _ok(L(f"🗑 removed {target}.{tail}", f"🗑 удалён {target}.{tail}"))

    if sub in ("enable", "disable"):
        entry = installed.get(target)
        if entry is None:
            return _err(L(f"“{target}” is not installed.", f"«{target}» не установлен."))
        manager.set_enabled(target, sub == "enable")
        state = (L("enabled", "включен") if sub == "enable" else L("disabled", "выключен"))
        return _ok(f"{TYPE_ICON.get(entry.get('type', ''), '')} {target} {state}."
                   f"{_reload_extensions(ctx)}")

    return _err("usage: /plugin <install|remove|list|enable|disable> [name]")


def _cmd_skills(ctx, args):
    loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
    skills = loader.skills if loader is not None else []
    if not skills:
        return CommandResult(output=Text(
            L("No skills installed. /plugins → the skills category.",
                "Скилы не установлены. /plugins → категория skills."), style="dim"))
    table = Table(title=bee_title(L("Skills", "Скилы")),
                  header_style="bold " + HONEY, border_style=BORDER,
                  box=box.SIMPLE_HEAVY)
    table.add_column("name", style="bold #ffcc00")
    table.add_column(L("description", "описание"))
    for s in skills:
        table.add_row(f"📚 {s.name}", s.description)
    table.caption = Text(L("open it: /skill <name>", "открыть: /skill <name>"), style="dim")
    return CommandResult(output=table)


def _cmd_skill(ctx, args):
    if not args:
        return _cmd_skills(ctx, [])
    loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
    skills = loader.skills if loader is not None else []
    name = " ".join(args)
    for s in skills:
        if s.name == name:
            from rich.markdown import Markdown
            return CommandResult(output=Panel(
                Markdown(s.body()),
                title=bee_title(f"📚 {s.name}"),
                border_style=BORDER, box=box.ROUNDED, padding=(0, 1),
            ))
    return _err(L(f"Skill “{name}” not found. /skills lists them.", f"Скил «{name}» не найден. /skills — список."))


def _cmd_mcp(ctx, args):
    manager, loader = _extensions(ctx)
    sub = args[0].lower() if args else "list"
    rest = args[1:]
    servers = manager.mcp_servers()

    if sub == "list":
        if not servers:
            return CommandResult(output=Text(
                L("No MCP servers configured. /plugins → the mcp category.",
                "MCP-серверы не настроены. /plugins → категория mcp."), style="dim"))
        table = Table(title=bee_title(L("MCP servers", "MCP-серверы")),
                      header_style="bold " + HONEY, border_style=BORDER,
                      box=box.SIMPLE_HEAVY)
        table.add_column("name", style="bold #ffcc00")
        table.add_column(L("command", "команда"))
        table.add_column(L("tools", "инструменты"), justify="right")
        table.add_column(L("status", "статус"), style="green")
        for name, cfg in servers.items():
            cached = _mcp_cached(loader, name)
            if not cfg.get("enabled", True):
                state = L("⏸ disabled", "⏸ выключен")
            elif cached:
                state = L("🟢 ready", "🟢 готов")
            else:
                state = L("⚪ run /mcp connect", "⚪ нужен /mcp connect")
            command = f"{cfg.get('command', '')} {' '.join(cfg.get('args', []) or [])}".strip()
            table.add_row(name, command, str(len(cached)), state)
        table.caption = Text("/mcp connect <name> · /mcp add <name> <cmd> [args]",
                             style="dim")
        return CommandResult(output=table)

    if sub == "tools":
        lines = []
        for name in (rest or list(servers)):
            cached = _mcp_cached(loader, name)
            if not cached:
                lines.append(L(f"{name}: (no cache — run /mcp connect {name})",
                           f"{name}: (нет кэша — /mcp connect {name})"))
                continue
            lines.append(f"{name}:")
            lines.extend(
                f"  - {t.get('name')}: {(t.get('description') or '')[:90]}"
                for t in cached)
        return CommandResult(output=Text("\n".join(lines) or L("(empty)", "(пусто)")))

    if sub == "connect":
        if not rest:
            return _err("usage: /mcp connect <name>")
        name = " ".join(rest)
        if name not in servers:
            return _err(L(f"MCP server “{name}” is not configured. /mcp list shows them.",
                      f"Сервер «{name}» не настроен. /mcp list — список."))
        if loader is None:
            return _err(L("Connecting MCP servers works in the interactive REPL.",
                      "Подключение MCP доступно в интерактивном режиме."))
        try:
            tools = loader.connect_mcp(name)
        except Exception as e:
            return _err(f"❌ {name}: {e}")
        ctx.agent.reload_extensions()
        return _ok(L(f"🔌 {name}: connected {len(tools)} tools.",
                     f"🔌 {name}: подключено {len(tools)} инструментов."))

    if sub == "add":
        if len(rest) < 2:
            return _err("usage: /mcp add <name> <command> [args...]")
        manager.add_mcp_server(rest[0], rest[1], rest[2:])
        return _ok(f"🔌 {rest[0]} → {rest[1]} {' '.join(rest[2:])}".rstrip()
                   + f".{_reload_extensions(ctx)}")

    if sub in ("remove", "uninstall"):
        if not rest:
            return _err("usage: /mcp remove <name>")
        name = " ".join(rest)
        if name not in servers:
            return _err(L(f"MCP server “{name}” is not configured.", f"Сервер «{name}» не настроен."))
        manager.uninstall(name)
        tail = _reload_extensions(ctx)
        return _ok(L(f"🗑 removed MCP server {name}.{tail}",
                     f"🗑 удалён MCP-сервер {name}.{tail}"))

    return _err("usage: /mcp <list|add|remove|connect|tools>")


def _mcp_cached(loader, name: str) -> list[dict]:
    if loader is None:
        return []
    return loader.mcp.cached_tools(name) or []


def _cmd_theme(ctx, args):
    if not args:
        return CommandResult(output=Text(f"current theme: {ctx.theme}  (see list below)", style="dim"))
    name = args[0]
    ctx.theme = name
    return _ok(f"theme → {name}")


def _cmd_bee(ctx, args):
    ctx.bee_enabled = not ctx.bee_enabled
    return _ok(f"bee animation: {'on' if ctx.bee_enabled else 'off'}")


def _cmd_clear(ctx, args):
    return CommandResult(action="clear")


def _cmd_quit(ctx, args):
    ctx.running = False
    return CommandResult(action="quit")


HANDLERS: dict[str, Callable] = {
    "help": _cmd_help,
    "about": _cmd_about,
    "models": _cmd_models,
    "providers": _cmd_providers,
    "model": _cmd_model,
    "provider": _cmd_provider,
    "lang": _cmd_lang,
    "mode": _cmd_mode,
    "thinking": _cmd_thinking,
    "tools": _cmd_tools,
    "plugins": _cmd_plugins,
    "plugin": _cmd_plugin,
    "skills": _cmd_skills,
    "skill": _cmd_skill,
    "mcp": _cmd_mcp,
    "config": _cmd_config,
    "run": _cmd_run,
    "read": _cmd_read,
    "search": _cmd_search,
    "find": _cmd_find,
    "status": _cmd_status,
    "diff": _cmd_diff,
    "log": _cmd_log,
    "token": _cmd_token,
    "stats": _cmd_stats,
    "history": _cmd_history,
    "session": _cmd_session,
    "sessions": _cmd_sessions,
    "save": _cmd_save,
    "export": _cmd_export,
    "continue": _cmd_continue,
    "load": _cmd_continue,
    "reset": _cmd_reset,
    "new": _cmd_reset,
    "theme": _cmd_theme,
    "bee": _cmd_bee,
    "clear": _cmd_clear,
    "quit": _cmd_quit,
}


def dispatch(ctx: ReplContext, line: str) -> CommandResult:
    parts = line.strip().split()
    if not parts:
        return CommandResult()
    name = parts[0][1:] if parts[0].startswith("/") else parts[0]
    args = parts[1:]
    handler = HANDLERS.get(name)
    if handler is None:
        return _err(f"Unknown command: /{name}. Type /help for the list.")
    return handler(ctx, args)
