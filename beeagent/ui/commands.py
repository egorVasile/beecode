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
from beeagent.core.permissions import AUTO, MODE_HELP, MODES as PERMISSION_MODES, READONLY
from beeagent.plugins.catalog import TYPE_ICON
from beeagent.ui.components import BORDER, HONEY, bee_title

AVAILABLE_MODES = ["normal", "economy"]

THEMES = [
    "textual-dark", "textual-light", "nord", "gruvbox",
    "catppuccin-mocha", "catppuccin-latte", "dracula",
    "monokai", "solarized-light", "flexoki",
]



@dataclass
class Command:
    name: str
    description: str
    arg: Optional[str] = None  # "model" | "provider" | "mode" | "session" | "theme" | "path" | None
    usage: Optional[str] = None
    category: str = "general"


def add_command(name: str, description: str, usage: str = "", category: str = "plugins") -> Command:
    """Advertise a command contributed by a plugin, so /help and completion see it."""
    command = Command(name=name, description=description, usage=usage or f"/{name}",
                      category=category)
    COMMANDS.append(command)
    return command


COMMANDS: list[Command] = [
    # help / info
    Command("help", "Show all available commands", category="info"),
    Command("about", "About BeeCode", category="info"),
    Command("config", "Show current configuration", category="info"),
    Command("tools", "List registered tools", category="info"),
    Command("stats", "Show economy/request stats", category="info"),
    Command("token", "Show current context token usage", category="info"),
    Command("thinking", "Show the last model reasoning (scrollable)", category="info"),
    Command("window", "Show or measure the model context window", usage="/window [measure] [model]", category="info"),
    # model / provider / mode
    Command("model", "Switch the active model", arg="model", usage="/model <name>", category="engine"),
    Command("models", "List models with the widest context first, --all for every one",
            arg="model", usage="/models [name|upstream] [--all]", category="engine"),
    Command("provider", "Switch the active provider", arg="provider", usage="/provider <name>", category="engine"),
    Command("providers", "List providers and which ones have a key", category="engine"),
    Command("key", "Store your own API key for a provider", usage="/key <provider> <token>", category="engine"),
    Command("mode", "Switch between normal and economy", arg="mode", usage="/mode <normal|economy>", category="engine"),
    Command("permissions", "Who may touch the machine: ask, auto or readonly", arg="permission",
            usage="/permissions <ask|auto|readonly>", category="engine"),
    Command("allow", "Grant one unsafe tool for this session", arg="tool",
            usage="/allow <tool>", category="engine"),
    Command("lang", "Switch the interface language", usage="/lang <en|ru>", category="engine"),
    Command("skin", "Choose interface variants: frames, banner, spinner", usage="/skin [slot] [variant]", category="engine"),
    Command("extensions", "What the installed plugins added", category="engine"),
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

def _cached_models(name: str, fetch, allow_fetch: bool, fallback: list[str]) -> list[str]:
    """Model list for an endpoint that reports its own catalogue.

    Completion runs on every keystroke, so it must never touch the network:
    only an explicit /models (or the picker) may fetch, everything else reads
    the cache the last explicit call wrote.
    """
    from time import time

    path = Path(".beeagent") / f"models_{name}.json"
    cached: list[str] = []
    fresh = False
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached = list(data.get("models", []))
            fresh = time() - float(data.get("saved_at", 0)) < 3600
        except (json.JSONDecodeError, OSError, ValueError):
            cached = []
    if cached and (fresh or not allow_fetch):
        return cached
    if not allow_fetch:
        return cached or fallback          # completion must work before any fetch
    try:
        from beeagent.plugins.mcp import run_coro_blocking

        # This runs on the thread that owns the prompt: every second here is a
        # second the interface cannot draw, answer or cancel. A slow endpoint is
        # not worth a frozen terminal — the cached list is served instead, and
        # the next call retries once the hour has passed.
        models = run_coro_blocking(fetch, timeout=6)
    except Exception:
        return cached or fallback
    if not models:
        return cached or fallback
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time(), "models": models}), encoding="utf-8")
    except OSError:
        pass
    return models


def available_models(ctx: ReplContext, fetch: bool = False) -> list[str]:
    """The models of the provider that is currently selected."""
    agent = ctx.agent
    if agent is None:
        from beeagent.providers.g4f_provider import G4fProvider
        return G4fProvider.discover_models()
    name = getattr(ctx.config, "provider", "") or "g4f"
    provider = agent.providers.get(name)
    if provider is None:
        return []
    discover = getattr(provider, "discover_models", None)
    if callable(discover):
        return list(discover())
    list_models = getattr(provider, "list_models", None)
    if callable(list_models):
        declared = list(getattr(provider, "models", None) or [])
        return _cached_models(name, lambda: list_models(), allow_fetch=fetch, fallback=declared)
    return list(getattr(provider, "models", None) or [])


def format_window(size: int) -> str:
    """A token count the way a model id writes it: 32k, 128k, 1M."""
    return f"{size // (1000 * 1000)}M" if size >= 1000 * 1000 else f"{size // 1000}k"


def model_choices(ctx: ReplContext) -> list:
    """Picker rows for /model: recommended first, each labelled with its window.

    The picker used to hand back the catalog in whatever order g4f listed it,
    so a model measured here at 2k sat above one that holds a whole session,
    and nothing told the user which was which.
    """
    from beeagent.core import windows
    from beeagent.core.context import advertised_window
    from beeagent.providers.g4f_provider import G4fProvider

    models = available_models(ctx, fetch=True)
    if (getattr(ctx.config, "provider", "") or "g4f") != "g4f":
        return list(models)

    ordered = G4fProvider.by_window(models)
    wide = set(G4fProvider.recommended_models(models=ordered))
    pairs = []
    for model in ordered:
        measured = windows.measured(model)
        label = (f"★ {model}" if model in wide else f"   {model}") + f"  ·  " \
                + ("✔ " if measured else "~ ") + format_window(advertised_window(model))
        pairs.append((model, label))
    return pairs


def available_providers(ctx: ReplContext) -> list[str]:
    from beeagent.providers.presets import BY_NAME

    names: list[str] = []
    agent = ctx.agent
    if agent is not None:
        names.extend(agent.providers.list_names())
    for builtin in ("g4f", "openai_compat", "ollama"):
        if builtin not in names:
            names.append(builtin)
    # Free-tier endpoints are always offered: picking one without a key gets a
    # message that says where to get the key.
    for name in BY_NAME:
        if name not in names:
            names.append(name)
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


def unsafe_tool_names(ctx: ReplContext) -> list[str]:
    """Tools `/allow` can grant — the ones that change the machine."""
    if ctx.agent is None:
        return []
    return [t.name for t in ctx.agent.tools.list_tools() if not t.is_safe()]


def build_sources(ctx: ReplContext) -> dict[str, list[str]]:
    return {
        "model": available_models(ctx),
        "provider": available_providers(ctx),
        "mode": list(AVAILABLE_MODES),
        "permission": list(PERMISSION_MODES),
        "tool": unsafe_tool_names(ctx),
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
        title_align="center",
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
    return CommandResult(output=Panel(text, title=bee_title("About"), title_align="center",
                                      border_style=BORDER, box=box.ROUNDED))


def _cmd_models(ctx, args):
    from beeagent.providers.g4f_provider import G4fProvider
    from beeagent.ui.components import models_table

    provider_name = getattr(ctx.config, "provider", "") or "g4f"
    models = available_models(ctx, fetch=True)
    if not models:
        return _err(L("this provider reported no models — /providers shows what is configured",
                      "провайдер не вернул моделей — что настроено, видно в /providers"))

    flags = ("-a", "--all")
    given = [a.lower() for a in args]
    show_all = any(a in flags for a in given)
    rest = [a for a in given if a not in flags]
    if rest and rest[0] in ("-u", "--upstream"):
        rest = rest[1:]
    query = rest[0] if rest else ""
    upstream_map = G4fProvider.upstream_map()
    if query:
        models = [m for m in models
                  if query in m.lower() or any(query == p.lower() for p in upstream_map.get(m, []))]
        if not models:
            return _err(L(f"nothing matches “{query}” — /models lists every model",
                          f"ничего не подходит под «{query}» — /models покажет все модели"))

    if provider_name != "g4f":
        return CommandResult(output=models_table(models))

    from beeagent.core import windows
    from beeagent.core.context import advertised_window

    total = len(models)
    models = G4fProvider.by_window(models)
    heading = L("biggest context first", "сначала с самым большим контекстом")
    if not query and not show_all:
        wide = set(G4fProvider.recommended_models(models=models))
        models = [m for m in models if m in wide]
        heading = L("recommended: widest context", "рекомендуемые: самый большой контекст")

    table = Table(title=bee_title(f"🐝 g4f models — {heading} "
                                 f"({len(models)} of {total})"),
                  box=box.ROUNDED, border_style=BORDER, header_style="bold " + HONEY,
                  expand=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("model", style="bold #ffcc00")
    table.add_column("window", justify="right")
    table.add_column("served by", style="dim")
    for i, model in enumerate(models, 1):
        measured = windows.measured(model)
        table.add_row(str(i), model,
                      ("✔ " if measured else "~ ") + format_window(advertised_window(model)),
                      ", ".join(upstream_map.get(model, [])[:3]) or "—")
    table.caption = Text(
        L("✔ window measured on this machine · ~ claimed by the model name · requests are capped "
          "at 32k tokens unless you raise max_context_tokens\n"
          "every model: /models --all · measure one: /window measure <model> · "
          "filter: /models <name|upstream> · switch: /model <name>",
          "✔ окно измерено на этой машине · ~ заявлено в имени модели · запросы режутся до 32k, "
          "пока не поднят max_context_tokens\n"
          "все модели: /models --all · померить: /window measure <модель> · "
          "фильтр: /models <имя|провайдер> · переключить: /model <имя>"),
        style="dim")
    return CommandResult(output=table)


def _cmd_providers(ctx, args):
    """What can serve requests right now, and what still needs a key."""
    from beeagent.providers.presets import ENDPOINTS, key_for
    from beeagent.ui.components import providers_table

    rows = [{"name": "g4f", "type": L("free, keyless", "бесплатно, без ключа"),
             "desc": L("public endpoints routed by g4f — works out of the box",
                       "публичные эндпоинты через g4f — работает сразу")}]
    active = getattr(ctx.config, "provider", "g4f")
    for endpoint in ENDPOINTS:
        ready = bool(key_for(endpoint, ctx.config.api_keys))
        mark = "  ←" if active == endpoint.name else ""
        detail = (L("✅ ready — ", "✅ готов — ") if ready
                  else L("⚪ no key, run: /key ", "⚪ нет ключа, добавь: /key "))
        rows.append({
            "name": endpoint.name + mark,
            "type": L("free tier + your own key", "бесплатный тариф + твой ключ"),
            "desc": detail + endpoint.name + " <token> · " + endpoint.free + " · " + endpoint.signup,
        })
    if ctx.agent is not None:
        for name in ctx.agent.providers.list_names():
            if name != "g4f" and not any(name == e.name for e in ENDPOINTS):
                rows.append({"name": name, "type": L("configured", "настроен"),
                             "desc": L("registered from beeagent.json",
                                       "зарегистрирован в beeagent.json")})
    return CommandResult(output=providers_table(rows))


def _cmd_key(ctx, args):
    """Store a key the user obtained themselves. The token is never echoed."""
    from beeagent.config.loader import save_config
    from beeagent.providers.openai_compat import OpenAICompatProvider
    from beeagent.providers.presets import BY_NAME

    def persist():
        try:
            save_config(ctx.config, ctx.agent.workdir if ctx.agent is not None else ".")
        except OSError:
            pass

    if not args:
        stored = sorted((ctx.config.api_keys or {}).keys())
        return CommandResult(output=Text(
            L(f"keys stored for: {', '.join(stored) or 'nobody yet'}   add one: /key <provider> <token>",
              f"ключи сохранены для: {', '.join(stored) or 'пока никого'}   добавить: /key <провайдер> <токен>"),
            style="dim"))

    name = args[0].lower()
    if name not in BY_NAME:
        return _err(L(f"unknown provider '{name}'. /providers lists them.",
                      f"неизвестный провайдер '{name}'. Список — /providers."))
    endpoint = BY_NAME[name]

    def drop():
        ctx.config.api_keys.pop(name, None)
        if ctx.agent is not None:
            ctx.agent.providers.unregister(name)
            ctx.agent.ready_presets = [p for p in ctx.agent.ready_presets if p != name]
        persist()

    # Bare `/key groq` reports the state instead of deleting anything: a missing
    # argument must never cost the user their key.
    if len(args) < 2:
        current = (ctx.config.api_keys or {}).get(name)
        if current:
            return CommandResult(output=Text(L(
                f"{endpoint.label}: key saved (…{current[-4:]}) — "
                f"replace it with /key {name} <token>, delete with /key {name} remove",
                f"{endpoint.label}: ключ сохранён (…{current[-4:]}) — "
                f"заменить: /key {name} <токен>, удалить: /key {name} remove"), style="dim"))
        return CommandResult(output=Text(L(
            f"{endpoint.label}: no key yet — get one at {endpoint.signup} "
            f"and run /key {name} <token>",
            f"{endpoint.label}: ключа нет — возьми на {endpoint.signup} "
            f"и выполни /key {name} <токен>"), style="dim"))

    if args[1] in ("remove", "rm", "delete"):
        drop()
        return _ok(L(f"🗑 key for {endpoint.label} removed", f"🗑 ключ {endpoint.label} удалён"))

    token = args[1].strip()
    ctx.config.api_keys[name] = token
    if ctx.agent is not None:
        ctx.agent.providers.register(OpenAICompatProvider(
            base_url=endpoint.url, api_key=token,
            model=endpoint.models[0] if endpoint.models else "gpt-4",
            name=name, models=endpoint.models))
        ctx.agent.ready_presets = list(dict.fromkeys(list(ctx.agent.ready_presets) + [name]))
    persist()
    return _ok(L(f"🔑 saved a key for {endpoint.label} (…{token[-4:]}). Activate: /provider {name}",
                 f"🔑 ключ {endpoint.label} сохранён (…{token[-4:]}). Включить: /provider {name}"))


def _persist_config(ctx) -> None:
    """Write the config the user just changed.

    `/lang` and `/permissions` saved; `/model`, `/provider` and `/mode` did not,
    so a restart came back on the previous model and the picker looked like it
    had forgotten what was chosen.
    """
    from beeagent.config.loader import save_config

    try:
        save_config(ctx.config, ctx.agent.workdir if ctx.agent is not None else ".")
    except OSError:
        pass


def _cmd_model(ctx, args):
    if not args:
        return CommandResult(output=Text(f"current model: {ctx.config.model}  (see /models)", style="dim"))
    # Two ids in the catalog contain a space ("Think Deeper"); taking args[0]
    # made them unreachable from the picker, which sends the whole name.
    name = " ".join(args).strip()
    if name not in available_models(ctx):
        return _err(f"Unknown model '{name}'. Run /models to see the list.")
    ctx.config.model = name
    if ctx.agent is not None:
        ctx.agent.context.model = name
    _persist_config(ctx)
    return _ok(f"model → {name}")


def _cmd_provider(ctx, args):
    from beeagent.providers.presets import BY_NAME, key_for

    if not args:
        return CommandResult(output=Text(
            L(f"current provider: {ctx.config.provider}  — /providers shows the rest",
              f"текущий провайдер: {ctx.config.provider}  — остальные в /providers"), style="dim"))
    name = args[0].lower()
    if name in BY_NAME and not key_for(BY_NAME[name], ctx.config.api_keys):
        return _err(L(f"{BY_NAME[name].label} needs a key of your own: /key {name} <token> "
                      f"(free at {BY_NAME[name].signup})",
                      f"{BY_NAME[name].label} нужен твой ключ: /key {name} <токен> "
                      f"(бесплатно на {BY_NAME[name].signup})"))
    if ctx.agent is not None and ctx.agent.providers.get(name) is None:
        return _err(L(f"provider '{name}' is not registered — /providers shows what works",
                      f"провайдер '{name}' не зарегистрирован — список в /providers"))
    ctx.config.provider = name
    if ctx.agent is not None and name != "g4f":
        models = getattr(ctx.agent.providers.get(name), "models", None) or []
        if models:
            ctx.config.model = models[0]
            ctx.agent.context.model = models[0]
    _persist_config(ctx)
    return _ok(L(f"provider → {name}   its models: /models",
                 f"провайдер → {name}   его модели: /models"))


def _cmd_skin(ctx, args):
    """Choose an interface variant, or show what is available.

    `/skin` lists the slots, `/skin frame none` takes the frames away,
    `/skin banner none` removes the animated logo, `/skin spinner dots` makes
    the waiting line quiet. The choice is saved, so it survives a restart.
    """
    from beeagent.ui import skin

    if not args:
        table = Table(title=bee_title("🐝 interface slots"), **skin.frame_kwargs(BORDER),
                      header_style="bold " + HONEY, expand=False)
        table.add_column("slot", style="bold #ffcc00")
        table.add_column("now")
        table.add_column("choices", style="dim")
        for slot in ("frame", "banner", "spinner", "stream"):
            table.add_row(slot, skin.get(slot), ", ".join(skin.variants(slot)))
        table.caption = Text(
            L("change one: /skin <slot> <variant> · back to defaults: /skin reset",
              "изменить: /skin <слот> <вариант> · вернуть как было: /skin reset"),
            style="dim")
        return CommandResult(output=table)

    if args[0] == "reset":
        skin.reset()
        ctx.config.ui = {}
        _persist_config(ctx)
        return _ok(L("interface slots are back to their defaults",
                     "слоты интерфейса вернули к значениям по умолчанию"))

    if len(args) < 2:
        return _err(L("usage: /skin <slot> <variant> — /skin lists them",
                      "использование: /skin <слот> <вариант> — список по /skin"))
    slot, name = args[0].lower(), args[1].lower()
    if not skin.choose(slot, name):
        return _err(L(f"no variant “{name}” for {slot} — /skin lists them",
                      f"нет варианта «{name}» для {slot} — список по /skin"))
    ui = dict(getattr(ctx.config, "ui", {}) or {})
    ui[slot] = name
    ctx.config.ui = ui
    _persist_config(ctx)
    if slot == "stream":
        # The renderer is a live object holding this turn's buffers; switching
        # the slot only takes effect on a fresh one.
        from beeagent.ui.components import reset_stream

        reset_stream()
    return _ok(L(f"{slot} → {name}", f"{slot} → {name}"))


def _cmd_extensions(ctx, args):
    """What every installed plugin actually added."""
    from beeagent.ui import skin
    from beeagent.ext.api import ExtensionRegistry

    registry = getattr(getattr(ctx.agent, "plugins", None), "extensions", None) or ExtensionRegistry()
    table = Table(title=bee_title("🐝 extensions"), **skin.frame_kwargs(BORDER),
                  header_style="bold " + HONEY, expand=False)
    table.add_column("plugin", style="bold #ffcc00")
    table.add_column("kind", style="dim")
    table.add_column("name")
    table.add_column("note", style="dim")
    for contribution in registry.contributions:
        table.add_row(contribution.plugin, contribution.kind, contribution.name, contribution.note)
    if not registry.contributions:
        table.add_row("—", "", L("nothing registered via the extension API yet",
                                 "через API расширений пока ничего не добавлено"),
                      L("a plugin exposes setup(api) to add commands, tools, "
                        "settings, events and interface variants",
                        "плагин объявляет setup(api) — так добавляют команды, инструменты, "
                        "настройки, события и варианты интерфейса"))
    errors = list(getattr(getattr(ctx.agent, "plugins", None), "load_errors", []) or [])
    if errors:
        table.caption = Text(L("failed to load: " + "; ".join(errors),
                               "не загрузились: " + "; ".join(errors)), style="red")
    return CommandResult(output=table)


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
        _persist_config(ctx)
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
    _persist_config(ctx)
    return _ok(f"mode → {mode}")


def _permissions_of(ctx: ReplContext):
    """The live gate when an agent is up; the config is its source of truth."""
    return ctx.agent.permissions if ctx.agent is not None else None


def _cmd_permissions(ctx, args):
    from beeagent.config.loader import save_config

    perms = _permissions_of(ctx)
    mode = perms.mode if perms is not None else ctx.config.permissions.mode

    if not args:
        table = Table(title=bee_title(f"🐝 permissions — {mode}"), box=box.ROUNDED,
                      border_style=BORDER, expand=False)
        table.add_column("mode", style="bold #ffcc00")
        table.add_column("what it does")
        for name in PERMISSION_MODES:
            table.add_row(name + ("  ←" if name == mode else ""), MODE_HELP[name])
        granted = sorted(perms.granted if perms is not None else ctx.config.permissions.allowed)
        table.add_row(L("granted by hand", "разрешено вручную"),
                      ", ".join(granted) or L("nothing yet", "пока ничего"))
        table.caption = Text(L("switch: /permissions <mode> · one tool at a time: /allow <tool>",
                               "переключить: /permissions <режим> · точечно: /allow <инструмент>"),
                             style="dim")
        return CommandResult(output=table)

    wanted = args[0].lower()
    if wanted not in PERMISSION_MODES:
        return _err(L(f"unknown mode '{wanted}' — choose between {', '.join(PERMISSION_MODES)}",
                      f"неизвестный режим '{wanted}' — выбирай между {', '.join(PERMISSION_MODES)}"))
    ctx.config.permissions.mode = wanted
    if perms is not None:
        perms.set_mode(wanted)
    try:
        save_config(ctx.config, ctx.agent.workdir if ctx.agent is not None else ".")
    except OSError:
        pass
    return _ok(L(f"permissions → {wanted} · {MODE_HELP[wanted]}",
                 f"разрешения → {wanted} · {MODE_HELP[wanted]}"))


def _cmd_allow(ctx, args):
    perms = _permissions_of(ctx)
    if perms is None:
        return _err(L("no agent here — permissions are read from beeagent.json "
                      "(permissions.allowed)",
                      "агента нет — разрешения читаются из beeagent.json "
                      "(permissions.allowed)"))

    if not args:
        granted = ", ".join(sorted(perms.granted)) or L("nothing yet", "пока ничего")
        return CommandResult(output=Text(L(
            f"allowed this session: {granted}   grant: /allow <tool> · "
            f"revoke: /allow remove <tool>",
            f"разрешено в сессии: {granted}   выдать: /allow <инструмент> · "
            f"отозвать: /allow remove <инструмент>"), style="dim"))

    name = args[0].lower()
    if name in ("remove", "rm", "delete"):
        if len(args) < 2:
            return _err(L("usage: /allow remove <tool>", "использование: /allow remove <инструмент>"))
        target = args[1].lower()
        if perms.revoke(target):
            ctx.agent.sync_config_permissions()
            return _ok(L(f"🗑 {target} is not allowed any more", f"🗑 {target} больше нельзя"))
        return _err(L(f"{target} was never granted", f"{target} и не был разрешён"))

    tool = ctx.agent.tools.get(name)
    if tool is None:
        return _err(L(f"no tool '{name}'. The ones that need a grant: "
                      f"{', '.join(unsafe_tool_names(ctx)) or '—'}",
                      f"нет инструмента '{name}'. Нужного в: "
                      f"{', '.join(unsafe_tool_names(ctx)) or '—'}"))
    if tool.is_safe():
        return _ok(L(f"{name} only reads — it never needed a grant",
                     f"{name} только читает — разрешение ему не нужно"))
    if perms.mode == READONLY:
        return _err(L("read-only mode ignores grants — run /permissions ask first",
                      "режим только чтения игнорирует разрешения — сначала /permissions ask"))
    perms.grant(name)
    ctx.agent.sync_config_permissions()
    return _ok(L(f"✅ {name} allowed for this session; write it into "
                 f"permissions.allowed in beeagent.json to keep it",
                 f"✅ {name} разрешён на эту сессию; впиши в permissions.allowed "
                 f"в beeagent.json, чтобы осталось"))


def _cmd_tools(ctx, args):
    if ctx.agent is None:
        return _err("No agent available.")
    from beeagent.ui.components import tools_table
    return CommandResult(output=tools_table(ctx.agent.tools.list_tools()))


def _redact_secrets(data: dict) -> dict:
    """`/config` output ends up in screenshots and chat logs — never print a token."""
    out = dict(data)
    keys = out.get("api_keys") or {}
    if keys:
        out["api_keys"] = {name: f"…{str(token)[-4:]}" for name, token in keys.items()}
    providers = out.get("custom_providers") or []
    out["custom_providers"] = [
        {**p, "key": f"…{str(p['key'])[-4:]}" if p.get("key") else ""}
        for p in providers
    ]
    return out


def _cmd_config(ctx, args):
    table = Table(title=bee_title("Configuration"), box=box.ROUNDED, border_style=BORDER, expand=False)
    table.add_column("Key", style="bold #ffcc00")
    table.add_column("Value")
    for k, v in _redact_secrets(ctx.config.model_dump()).items():
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
    context = ctx.agent.context
    msgs = context.build_messages(ctx.session.to_dicts(), ctx.agent.tools.to_schemas())
    # Sum the messages themselves: counting json.dumps() charges ~3 tokens per
    # Cyrillic letter for the \\uXXXX escaping the model never sees.
    n = sum(count_tokens(str(m.get("content") or ""), ctx.config.model) for m in msgs)
    limit = context.max_tokens
    pct = int(100 * n / limit) if limit else 0
    text = Text()
    text.append(f"context tokens: ", style="dim")
    text.append(f"{n}", style="bold #ffcc00")
    text.append(f" / {limit}  ({pct}%)\n", style="dim")
    text.append(L(f"model {ctx.config.model} · window {context.window} · "
                  f"messages: {len(ctx.session.messages)}",
                  f"модель {ctx.config.model} · окно {context.window} · "
                  f"сообщений: {len(ctx.session.messages)}"), style="dim")
    if context.trimmed:
        text.append("\n" + L(f"{context.trimmed} of them are compressed into the "
                             f"summary in the system prompt",
                             f"{context.trimmed} из них сжаты в конспект в системном промпте"),
                    style="dim")
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


def _cmd_window(ctx, args):
    """What we believe about this model's context window, or measure it."""
    from beeagent.core import windows
    from beeagent.core.context import window_for

    if args and args[0] == "measure":
        rest = args[1:]
        model = " ".join(rest) if rest else ctx.config.model
        if ctx.agent is None:
            return _err(L("measuring needs a running agent (start beecode)",
                          "замер нужен работающий агент (запусти beecode)"))
        try:
            provider = ctx.agent.providers.select(ctx.config.provider)
        except Exception as e:
            return _err(str(e))

        def progress(size, accepted):
            console.print(L(f"  ⏳ probe {size} tokens (last accepted {accepted})...",
                            f"  ⏳ пробуем {size} токенов (последнее влезшее {accepted})..."),
                        style="dim")

        from beeagent.plugins.mcp import run_coro_blocking

        try:
            result = run_coro_blocking(
                lambda: windows.probe(model, provider, on_step=progress), timeout=1800)
        except Exception as e:
            return _err(L(f"measurement failed: {e}", f"замер не удался: {e}"))
        if not result.window:
            return _err(L(f"no window measured — {result.note}",
                          f"окно не измерено — {result.note}"))
        found = result.window
        return _ok(L(f"📏 {model}: window ≈ {found} tokens (measured, saved to "
                     f"{windows.CACHE})",
                     f"📏 {model}: окно ≈ {found} токенов (замерено, сохранено в {windows.CACHE})"))

    model = " ".join(args) if args else ctx.config.model
    measured = windows.measured(model)
    used = window_for(model)
    source = (L("measured at this endpoint", "замерено на этом эндпоинте") if measured
              else L("guessed from the model name", "угадано по имени модели"))
    ceiling = ctx.agent.context.max_tokens if ctx.agent is not None else used
    lines = Text()
    lines.append(f"{model}\n", style="bold #ffcc00")
    lines.append(L(f"  context window: {used} tokens  ({source})\n",
                   f"  контекстное окно: {used} токенов  ({source})\n"))
    lines.append(L(f"  request ceiling : {ceiling}\n", f"  потолок запроса  : {ceiling}\n"),
                 style="dim")
    lines.append(L("  measure it: /window measure <model>",
                   "  померить: /window measure <модель>"), style="dim")
    return CommandResult(output=lines)


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
    # --trust is a flag, not part of the name or the URL.
    trusted = "--trust" in rest
    target = " ".join(w for w in rest if w != "--trust")

    if sub == "install":
        # A git source is someone else's Python: the loader exec_module()s it on
        # the next start, so cloning it needs an explicit act of trust.
        if not target:
            return _err("usage: /plugin install <name|git-url> [--trust]")
        item = manager.catalog.get(target) or manager.catalog.find_git(target)
        if item is not None and item.source.get("kind") == "git" and not trusted:
            return _err(L(f"“{target}” installs external code that runs inside BeeCode as tools. "
                          f"Read it first, then re-run: /plugin install {target} --trust",
                          f"«{target}» ставит чужой код, который исполняется внутри BeeCode "
                          f"как инструменты. Прочитай его, потом: /plugin install {target} --trust"))
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
                title=bee_title(f"📚 {s.name}"), title_align="center",
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
    "key": _cmd_key,
    "model": _cmd_model,
    "provider": _cmd_provider,
    "lang": _cmd_lang,
    "skin": _cmd_skin,
    "extensions": _cmd_extensions,
    "mode": _cmd_mode,
    "permissions": _cmd_permissions,
    "allow": _cmd_allow,
    "thinking": _cmd_thinking,
    "window": _cmd_window,
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
