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
from beeagent.utils.sanitize import strip_terminal

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


def drop_command(name: str) -> None:
    """Take an extension's command back out: it lives in two places at once.

    Only a command that was contributed is removable. Without that filter, the
    bookkeeping of one plugin could delete a command BeeCode ships — and a
    refused registration is not a contribution, whatever the record says.
    """
    contributed = [c for c in COMMANDS if c.name == name and c.category == "plugins"]
    if not contributed:
        return
    COMMANDS[:] = [c for c in COMMANDS
                   if not (c.name == name and c.category == "plugins")]
    HANDLERS.pop(name, None)


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
    Command("update", "Check for a newer BeeCode and install it", category="info"),
    Command("pool", "Address, seat and budget of a key pool", usage="/pool [url <адрес> | enroll | status]", category="info"),
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
    Command("stop", "Interrupt the answer that is being written now", category="session"),
    Command("tasks", "Show the task list the agent is keeping", category="session"),
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

    from beeagent import __version__

    path = Path(".beeagent") / f"models_{name}.json"
    cached: list[str] = []
    fresh = False
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            cached = list(data.get("models", []))
            # An update can change what a provider answers for -- a model list
            # written by the previous version is not a fact about this one, and
            # waiting out an hour of cache after `--update` is how a fixed list
            # looks like it never got fixed.
            fresh = (time() - float(data.get("saved_at", 0)) < 3600
                     and data.get("version") == __version__)
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
        path.write_text(json.dumps({"saved_at": time(), "version": __version__,
                                    "models": models}), encoding="utf-8")
    except OSError:
        pass
    return models


async def _off_loop(fn):
    """A blocking call, kept off the loop that draws the interface."""
    import asyncio
    return await asyncio.to_thread(fn)


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
        if name == "g4f":
            # g4f assembles its catalogue from the installed package: no request,
            # nothing to cache. Every other `discover_models` is an HTTP GET on an
            # endpoint that counts it against a per-minute limit, so it goes
            # through the same hour-old cache as the rest and only runs when the
            # user asked for the list — chat is the only thing that leaves.
            return list(discover())
        declared = list(getattr(provider, "models", None) or [])
        return _cached_models(name, lambda: _off_loop(discover),
                              allow_fetch=fetch, fallback=declared)
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
        # The star is a judgement about the g4f catalogue -- "measured to hold a
        # wide session" -- and no other provider earns it by existing here. The
        # window still belongs on the row: it is keyed by model id, and a bare
        # name made every non-g4f list look like a different, dumber screen.
        return [(model, f"   {model}  ·  "
                        + ("✔ " if windows.measured(model) else "~ ")
                        + format_window(advertised_window(model)))
                for model in models]

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
    """The two providers BeeCode is offered as: keyless g4f, and our own pool.

    The list used to carry every free-tier endpoint that takes a key of your own
    (crax, groq, openrouter, ollama, …), which is a different product promise from
    the one on the box. A key already stored in beeagent.json keeps working, and
    `/provider <name>` still accepts a registered name -- what is gone is offering
    a stranger ten rows that all say "go get a token first".
    """
    names = ["g4f", "pool"]
    for name in getattr(ctx.config, "custom_providers", []) or []:
        if name.name not in names:
            names.append(name.name)
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


def skin_choices(ctx: ReplContext) -> list:
    """The skin list as numbered rows, for the mouse picker and for `/skins 2`.

    The number is in the label because the dialog is the only place most users see
    the list; typing `/skins 2` has to pick the row they just looked at, not the
    same list in another order.
    """
    from beeagent.core import skins

    rows = skins.choice_rows()
    return [(value, f"{number} · {label}")
            for number, (value, label) in enumerate(rows, start=1)]


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
    # `/run` and `/read` reach the same tools the model does, so the mode the
    # user chose has to hold here too — otherwise `/permissions readonly` is a
    # promise the shell can walk straight past.
    if not ctx.agent.permissions.allows(tool):
        return _err(ctx.agent.permissions.refusal(tool))
    try:
        res = tool.execute(**kwargs)
    except Exception as e:
        return _err(f"{tool_name} failed: {e}")
    output = strip_terminal(res.output or "(no output)")
    return CommandResult(output=_tool_panel(title, output, res.error))


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
        from beeagent.core.context import advertised_window
        return CommandResult(output=models_table(
            sorted(models, key=advertised_window, reverse=True), provider=provider_name))

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
    from beeagent.ui.components import providers_table

    active = getattr(ctx.config, "provider", "g4f")
    about = {
        "g4f": (L("free, keyless", "бесплатно, без ключа"),
                L("public endpoints through g4f — works out of the box",
                  "публичные эндпоинты через g4f — работает сразу")),
        "pool": (L("free, keyless — our own server", "бесплатно, без ключа — наш сервер"),
                 L("your own BeeCode server holding the account keys; /pool enroll takes a seat",
                   "твой сервер BeeCode, ключи на нём; место берётся командой /pool enroll")),
    }
    rows = []
    for name in available_providers(ctx):
        kind, desc = about.get(name, (L("registered", "зарегистрирован"),
                                      L("from beeagent.json", "из beeagent.json")))
        rows.append({"name": name + ("  ←" if active == name else ""),
                     "type": kind, "desc": desc})
    return CommandResult(output=providers_table(rows))


def _cmd_key(ctx, args):
    """Store a key the user obtained themselves. The token is never echoed."""
    from beeagent.config.loader import save_config
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
        # Through the agent's factory, so an endpoint with its own provider class
        # keeps the behaviour that class carries.
        ctx.agent.attach_preset(name, token)
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
    # A picker sends the whole name, and model ids may contain a space, so the
    # arguments are rejoined rather than taken one at a time. The current g4f
    # catalogue happens to have none, but cutting at the first space would make
    # any such id unreachable the day one appears.
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
        # Say out loud that nothing changed. The refusal alone let a person open
        # /model, see another provider's names, and conclude the list was broken.
        return _err(L(f"{BY_NAME[name].label} needs a key of your own: /key {name} <token> "
                      f"(free at {BY_NAME[name].signup}) — still on {ctx.config.provider}",
                      f"{BY_NAME[name].label} нужен твой ключ: /key {name} <токен> "
                      f"(бесплатно на {BY_NAME[name].signup}) — ты всё ещё на {ctx.config.provider}"))
    if ctx.agent is not None and ctx.agent.providers.get(name) is None:
        return _err(L(f"provider '{name}' is not registered — /providers shows what works",
                      f"провайдер '{name}' не зарегистрирован — список в /providers"))
    ctx.config.provider = name
    # The model has to move with the provider in both directions. This used to
    # happen only when leaving g4f, so `/provider pool` then `/provider g4f` left
    # the session asking g4f for a crax model id it cannot serve.
    provider = ctx.agent.providers.get(name) if ctx.agent is not None else None
    models = list(getattr(provider, "models", None) or [])
    if not models and name == "g4f":
        from beeagent.providers.g4f_provider import G4fProvider
        models = G4fProvider.discover_models()
    if models:
        ctx.config.model = models[0]
        if ctx.agent is not None:
            ctx.agent.context.model = models[0]
    _persist_config(ctx)
    return _ok(L(f"provider → {name} · model → {ctx.config.model}   all of them: /models",
                 f"провайдер → {name} · модель → {ctx.config.model}   все: /models"))


def _skin_slot_table(skin, only: str = "") -> Table:
    """The slot board: what each slot is set to, and what it can be set to.

    `only` narrows it to one slot, which is what `/skin frame` asks for: the
    variants of that one thing, not the whole board again.
    """
    title = L(f"🐝 interface slot: {only}", f"🐝 слот интерфейса: {only}") if only \
        else bee_title("🐝 interface slots")
    table = Table(title=title, **skin.frame_kwargs(BORDER),
                  header_style="bold " + HONEY, expand=False)
    table.add_column("slot", style="bold #ffcc00")
    table.add_column("now")
    table.add_column("choices", style="dim")
    for slot in (["frame", "banner", "spinner", "stream"] if not only else [only]):
        table.add_row(slot, skin.get(slot), ", ".join(skin.variants(slot)))
    table.caption = Text(
        L("change one: /skin <slot> <variant> · back to defaults: /skin reset",
          "изменить: /skin <слот> <вариант> · вернуть как было: /skin reset"),
        style="dim")
    return table


def _cmd_skin(ctx, args):
    """Choose an interface variant, or show what is available.

    `/skin` lists the slots, `/skin frame none` takes the frames away,
    `/skin banner none` removes the animated logo, `/skin spinner dots` makes
    the waiting line quiet. The choice is saved, so it survives a restart.
    """
    from beeagent.ui import skin

    if not args:
        return CommandResult(output=_skin_slot_table(skin))

    if args[0] == "reset":
        skin.reset()
        ctx.config.ui = {}
        _persist_config(ctx)
        return _ok(L("interface slots are back to their defaults",
                     "слоты интерфейса вернули к значениям по умолчанию"))

    if len(args) < 2:
        asked = str(args[0] if args else "").strip().lower()
        if asked in ("frame", "banner", "spinner", "stream"):
            return CommandResult(output=_skin_slot_table(skin, asked))
        from beeagent.core import skins

        if asked and skins.resolve(asked):
            # He named a code skin at the slot command. The two commands look alike
            # and do different jobs, so the wrong door has to say where the right
            # one is rather than recite its own usage.
            return _err(L(f"“{asked}” is a skin of code, not a slot — wear it with: "
                          f"/skins {asked}",
                          f"«{asked}» — скин кода, а не слот; надеть: /skins {asked}"))
        return _err(L("usage: /skin <slot> <variant> — /skin lists them; a skin you "
                      "installed is worn with /skins <name>",
                      "использование: /skin <слот> <вариант> — список по /skin; "
                      "установленный скин надевается так: /skins <имя>"))
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
        if getattr(tool, "writes_files", False):
            # `todo` and `diagram` need no grant, but they are not readers either;
            # calling them "only reads" would hide why readonly mode stops them.
            return _ok(L(f"{name} runs without a grant — it does write a file of its "
                         f"own, so /permissions readonly still stops it",
                         f"{name} работает без разрешения — но он пишет свой файл, "
                         f"поэтому /permissions readonly его остановит"))
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
    """`/config` output ends up in screenshots and chat logs — never print a token.

    Anything whose name says key, token or secret is reduced to its tail, so a
    field added tomorrow is redacted by default rather than exposed by default.
    """
    out = dict(data)
    keys = out.get("api_keys") or {}
    if keys:
        out["api_keys"] = {name: f"…{str(token)[-4:]}" for name, token in keys.items()}
    providers = out.get("custom_providers") or []
    out["custom_providers"] = [
        {**p, "key": f"…{str(p['key'])[-4:]}" if p.get("key") else ""}
        for p in providers
    ]
    for name, value in list(out.items()):
        if isinstance(value, dict):
            # `extensions` is a dict of whatever a plugin stores, and a plugin
            # that keeps a token there would otherwise print it in full to a
            # `/config` screenshot.
            out[name] = _redact_secrets(value)
        elif any(part in name.lower() for part in ("key", "token", "secret", "password")) \
                and isinstance(value, str) and value:
            out[name] = f"…{value[-4:]}"
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


# How long `/stats --live` waits for the pool, per request. The seat ask is
# inside `pool_status`, which also pings `/healthz`, so the worst case a person
# who typed the flag waits is twice this. Nothing on this path runs unless asked:
# the boot that once took 57 seconds is the reason.
SEAT_LIVE_TIMEOUT = 4.0


def _big(number) -> str:
    """A count with its digit groups apart: 12345 is hard to read, 12 345 is not."""
    try:
        return f"{int(number):,}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def _duration(seconds) -> str:
    """How long an endpoint kept an answer waiting, or "—" when nothing timed it."""
    try:
        left = float(seconds)
    except (TypeError, ValueError):
        return "—"
    if left <= 0:
        return "—"
    if left < 100:
        return f"{left:.1f}s"
    minutes, secs = divmod(int(round(left)), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _plural(count, english_one, english_many, russian_one, russian_many) -> str:
    """One word or its plural, in the language being read.

    Russian needs the case as well as the number, so the caller hands both forms
    rather than this function guessing a suffix that never works.
    """
    one = abs(int(count or 0)) % 10 == 1 and abs(int(count or 0)) % 100 != 11
    return L(english_one if one else english_many, russian_one if one else russian_many)


def _counts_row(row: dict) -> tuple:
    """The six numbers every ledger row carries, in the order every table shows them.

    Tokens are printed with the mark the row earned: `✔` when the endpoint said
    how many it spent, `~` when BeeCode counted them itself — the same two marks
    `/models` puts in front of a context window.
    """
    from beeagent.core import usage

    tokens = int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)
    return (
        _big(row.get("requests")),
        f"{usage.mark_for(row)} {_big(row.get('prompt_tokens'))}",
        _big(row.get("completion_tokens")),
        _big(row.get("cache_hits")),
        _duration(row.get("stream_seconds")),
        _big(row.get("errors")),
    )


STATS_COLUMNS = ("model", "provider", "requests", "prompt", "answer",
                 "cache hits", "time", "errors")


def _counts_table(title: str, rows: list, first: str = "model") -> Table:
    """One table of ledger rows, honey on leaf, biggest already first."""
    table = Table(title=bee_title(title), box=box.ROUNDED, border_style=BORDER,
                  header_style="bold " + HONEY, expand=False)
    table.add_column(first, style="bold " + HONEY)
    table.add_column("provider", style="dim")
    for name in STATS_COLUMNS[2:]:
        table.add_column(name, justify="right")
    for row in rows:
        table.add_row(str(row.get("model") or "?"), str(row.get("provider") or "—"),
                      *_counts_row(row))
    return table


def _seat_live(ctx) -> tuple[list[tuple[str, str]], str]:
    """What the pool says this seat has spent, or the one line explaining why not.

    Only `/stats --live` gets here. A seat ask is one GET on an address that may
    be a free instance asleep since yesterday, and the local totals above it are
    true whether or not it answers — so a refusal costs a line and nothing else.
    """
    from beeagent.providers import pool as pool_mod

    provider = str(getattr(ctx.config, "provider", "") or "")
    if provider != "pool":
        return [], L(f"--live asks a pool seat; this BeeCode is answering from "
                     f"{provider or 'somewhere else'}, where a request costs nothing",
                     f"--live спрашивает место в пуле; сейчас ответы идут от "
                     f"{provider or 'неизвестно чего'}, где запрос ничего не стоит")

    url = str(getattr(ctx.config, "pool_url", "") or "")
    token = str(getattr(ctx.config, "pool_token", "") or "")
    if not url:
        return [], L("the pool has no address — /pool url https://…",
                     "у пула нет адреса — /pool url https://…")
    if not token:
        return [], L("this install holds no seat yet — /pool enroll takes one; "
                     "the numbers above are unaffected",
                     "это BeeCode ещё не имеет места в пуле — возьми его через "
                     "/pool enroll; цифры выше от этого не меняются")

    try:
        health = pool_mod.pool_status(url, token, timeout=SEAT_LIVE_TIMEOUT)
    except Exception as e:                      # a sleeping box, a wrong address
        return [], L(f"the pool at {url} did not answer within "
                     f"{SEAT_LIVE_TIMEOUT:.0f}s ({e.__class__.__name__}) — the "
                     f"numbers above are BeeCode's own count, not the seat's",
                     f"пул по адресу {url} не ответил за {SEAT_LIVE_TIMEOUT:.0f} с "
                     f"({e.__class__.__name__}) — цифры выше считает BeeCode, а не место")

    seat = health.get("seat") if isinstance(health.get("seat"), dict) else None
    if not seat or not seat.get("ok"):
        reason = str((seat or {}).get("error") or L("the pool answered nothing about "
                                                    "this seat", "пул ничего не сказал "
                                                    "об этом месте"))
        return [], reason

    rows: list[tuple[str, str]] = []
    used, limit = seat.get("requests"), seat.get("requests_limit")
    if isinstance(used, (int, float)):
        if isinstance(limit, (int, float)) and limit:
            left = int(limit) - int(used)
            bar = f"{int(100 * min(max(1 - left / limit, 0), 1))}%"
            rows.append(("seat requests", f"{_big(used)} / {_big(limit)}"))
            rows.append(("left today", f"{_big(max(0, left))} requests  ({bar} spent)"))
        else:
            rows.append(("seat requests", f"{_big(used)} (the pool named no limit)"))
    tokens, tokens_limit = seat.get("tokens"), seat.get("tokens_limit")
    if isinstance(tokens, (int, float)) and isinstance(tokens_limit, (int, float)):
        rows.append(("seat tokens", f"{_big(tokens)} / {_big(tokens_limit)}"))
    reset = seat.get("resets_in_seconds")
    if isinstance(reset, (int, float)) and reset > 0:
        import time as _clock

        when = _clock.localtime(_clock.time() + float(reset))
        rows.append(("resets in", f"{_duration(reset)} "
                     f"(at {_clock.strftime('%H:%M', when)})"))
    if seat.get("approved") is False:
        rows.append(("approval", L("the pool owner has not approved this seat yet",
                                   "владелец пула ещё не подтвердил это место")))
    if not rows:
        return [], L("the pool answered for this seat and named no budget in it",
                     "пул ответил за это место, но нормы в ответе не назвал")
    return rows, ""


def _cmd_stats(ctx, args):
    """`/stats` — the cost of this conversation and of every one before it.

    Free models are not free of everything: a seat has a daily request budget and
    a g4f route has patience, and the number worth seeing before a task ends is
    how much of either is left. `--live` is the only branch that asks the pool,
    and it is only ever reached by a person typing it.
    """
    if ctx.agent is None:
        return _err("No agent available.")
    from rich.console import Group

    from beeagent.core import usage

    given = [str(a).lower() for a in args]
    flags = [a for a in given if a.startswith("-")]
    live = any(a in ("-l", "--live") for a in given)

    s = ctx.agent.economy.get_stats()
    session_id = str(getattr(ctx.session, "session_id", "") or "")
    snap = usage.snapshot(session_id)

    table = Table(title=bee_title("🐝 Stats"), box=box.ROUNDED, border_style=BORDER,
                  header_style="bold " + HONEY, expand=False)
    table.add_column("Metric", style="bold " + HONEY)
    table.add_column("Value")
    # Which session these numbers belong to: a cache ratio and a pruned count say
    # nothing without the model, its provider and the window it was sized to.
    table.add_row("model", str(ctx.config.model))
    table.add_row("provider", str(ctx.config.provider))
    table.add_row("window", str(ctx.agent.context.window))
    if live:
        rows, refusal = _seat_live(ctx)
        for name, value in rows:
            table.add_row(name, value)
        if refusal:
            table.add_row("seat", refusal)
    else:
        table.add_row("seat budget", L("not asked — /stats --live asks the pool, "
                                       "and waits for its answer",
                                       "не спрашивали — /stats --live спросит пул "
                                       "и подождет его ответа"))

    parts: list = []
    notice = usage.take_notice()
    if notice:
        parts.append(Text(notice if notice.startswith("⚠") else "⚠ " + notice,
                          style="bold yellow"))
    # The economy rows stay where they always were: what a cache is holding and
    # what it pruned is part of the same question the ledger answers.
    for key, value in s.items():
        table.add_row(str(key), str(value))
    parts.append(table)

    mine = snap["session"] or {}
    parts.append(_counts_table("🐝 This conversation", [
        dict(mine, model=session_id or L("this conversation", "этот разговор"),
             provider=str(ctx.config.provider))], first="conversation"))

    models = snap["models"]
    shown = models[:usage.TOP_ROWS]
    total = snap["all"]
    if models:
        every = _counts_table("🐝 All BeeCode has asked here", shown)
        every.caption = Text(
            " + ".join([
                L(f"{_big(total.get('requests'))} "
                  f"{_plural(total.get('requests'), 'request', 'requests', 'запрос', 'запросов')}",
                  f"{_big(total.get('requests'))} "
                  f"{_plural(total.get('requests'), 'request', 'requests', 'запрос', 'запросов')}"),
                L(f"{usage.mark_for(total)} tokens {_big(total.get('prompt_tokens'))}"
                  f" in · {_big(total.get('completion_tokens'))} out",
                  f"{usage.mark_for(total)} токенов {_big(total.get('prompt_tokens'))}"
                  f" вошло · {_big(total.get('completion_tokens'))} вышло"),
                L(f"{_big(total.get('cache_hits'))} answered from the cache",
                  f"{_big(total.get('cache_hits'))} из кэша"),
                L(f"{_big(total.get('errors'))} refused",
                  f"{_big(total.get('errors'))} отказов"),
            ]), style="dim")
        parts.append(every)
    else:
        # An empty box is not an answer. Nothing recorded is a fact worth saying
        # in words, and saying it stops `/stats` looking like a broken screen.
        parts.append(Text(L("the ledger is empty: no answer has been counted in this "
                            "folder yet. Ask something, then look again",
                            "учёт пуст: в этой папке ещё не посчитано ни одного "
                            "ответа. Спроси что-нибудь и посмотри снова"), style="dim"))

    slow = snap["slowest"]
    tail = [L("✔ tokens the endpoint reported · ~ tokens BeeCode counted itself "
              "(the ruler is the one /token budgets a prompt with; it under-reads "
              "emoji and CJK where there is no tiktoken, as on Android)",
              "✔ токены назвал сам провайдер · ~ посчитал BeeCode (той же меркой, "
              "что и /token; без tiktoken — на Android — эмодзи и азиатские "
              "письмена он читает меньше, чем они стоят)")]
    if len(models) > len(shown):
        extra = len(models) - len(shown)
        tail.append(L(f"… and {extra} more "
                      f"{_plural(extra, 'model', 'models', 'модель', 'моделей')} this "
                      f"ledger knows but did not print",
                      f"… и ещё {extra} "
                      f"{_plural(extra, 'model', 'models', 'модель', 'моделей')}, "
                      f"которые учёт помнит, но сюда не выписал"))
    if slow:
        tail.append(L(f"slowest here: {slow['model']} "
                      f"{_duration(slow['seconds_per_request'])} an answer, over "
                      f"{_big(slow['requests'])} "
                      f"{_plural(slow['requests'], 'request', 'requests', 'запрос', 'запросов')}",
                      f"медленнее всех: {slow['model']} — "
                      f"{_duration(slow['seconds_per_request'])} на ответ, "
                      f"{_big(slow['requests'])} "
                      f"{_plural(slow['requests'], 'request', 'requests', 'запрос', 'запросов')}"))
    if snap["refusal"]:
        tail.append(L(f"last refusal: {snap['refusal']}",
                      f"последний отказ: {snap['refusal']}"))
    if snap["trimmed"]:
        tail.append(L(f"{snap['trimmed']} history "
                      f"{_plural(snap['trimmed'], 'row was', 'rows were', 'запись', 'записей')}"
                      f" dropped, to keep the ledger inside {usage.MAX_ROWS} models "
                      f"and {usage.MAX_SESSIONS} conversations",
                      f"учёт убран до {usage.MAX_ROWS} моделей и "
                      f"{usage.MAX_SESSIONS} разговоров: "
                      f"{_big(snap['trimmed'])} "
                      f"{_plural(snap['trimmed'], 'row', 'rows', 'запись стёрта', 'записей стёрто')}"
                      ))
    parts.append(Text("\n".join(tail), style="dim"))
    return CommandResult(output=Group(*parts))


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


def _wear_pack_skin(ctx, pack: str) -> str:
    """What to do with a skin pack the user just installed.

    Someone who installs `skin-pulse` off the shelf did it to *wear* it, so if
    nothing is chosen yet this puts it on and saves the choice. If he already wears
    something, that pick is his, not ours to overwrite: the line names the command
    instead.

    Three other endings have to be told apart, because "I installed a skin and
    nothing happened" was reported as all three at once: the pack registered no
    skin and no slot either (say nothing — it is a tool, not the interface), the
    folder's gate stopped its `plugin.py` from running (say that, and the command
    that fixes it), and a pack that really does own only slots (send him to `/skin`,
    where those live).
    """
    from beeagent.core import skins

    found = skins.for_pack(pack)
    if not found:
        loader = getattr(ctx.agent, "plugins", None) if ctx.agent is not None else None
        if loader is not None and loader.is_withheld("plugin", pack):
            return L(f"\n  ⚠ nothing is worn yet: this folder is not trusted, so BeeCode "
                     f"read “{pack}” but never ran it. /trust yes loads it, and the skin "
                     f"appears in /skins.",
                     f"\n  ⚠ скин не надет: папка не доверена, BeeCode прочитал «{pack}», "
                     f"но не запускал его. /trust yes — и скин появится в /skins.")
        if loader is not None and not any(
                c.kind == "skin" for c in loader.extensions.by_plugin(pack)):
            return ""             # a tool that happens to be a plugin: not the interface
        return L("\n  🎨 this pack owns interface slots, not a skin of its own: "
                 "look at /skin",
                 "\n  🎨 у этого пака своего скина нет, он меняет слоты: смотри /skin")
    if len(found) == 1 and not str(getattr(ctx.config, "skin", "") or "").strip():
        if skins.switch(found[0]) == "":
            ctx.config.skin = found[0]
            _persist_config(ctx)
            note = skins.visible_note(found[0])
            return L(f"\n  🎨 skin on: {found[0]}" + (f" — {note}" if note else ""),
                     f"\n  🎨 скин надет: {found[0]}" + (f" — {note}" if note else ""))
    return L(f"\n  🎨 skins in this pack: {', '.join(found)} · wear one: /skins {found[0]}"
             f" · the shelf name works too: /skins {pack}",
             f"\n  🎨 скины в паке: {', '.join(found)} · надеть: /skins {found[0]}"
             f" · можно и именем с полки: /skins {pack}")


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
    """Browse (or filter) the installable catalog of skills/plugins/MCP.

    The shipped catalog answers with no network at all. `market` as the first word
    asks the pool for its index instead -- the only branch here that reaches
    outward, on an explicit ask, and it degrades back to this catalog when the
    market cannot be reached.
    """
    if args and args[0].lower() in MARKET_WORDS:
        return _cmd_market(ctx, args[1:])
    manager, _ = _extensions(ctx)
    if args:
        query = " ".join(args)
        items = manager.catalog.items(query) or manager.catalog.search(query)
        title = L(f"Catalog: {query}", f"Каталог: {query}")
    else:
        items = manager.catalog.items()
        title = L("Catalog: skills, plugins and MCP servers", "Каталог: скилы, плагины и MCP-серверы")
    if not items:
        return _err(L("Nothing found. /plugins shows the whole catalog, "
                      "/plugins market the pool's index.",
                      "Ничего не найдено. /plugins — весь каталог, /plugins market — "
                      "индекс пула."))
    table = _catalog_table(items, manager.installed(), title)
    table.caption = Text(
        L("install: /plugin install <name> · pick with the mouse: /plugins"
          " · the pool's licence-gated index: /plugins market",
          "установить: /plugin install <name> · выбор мышкой: /plugins"
          " · индекс пула с фильтром по лицензии: /plugins market"), style="dim")
    return CommandResult(output=table)


# --- the remote market ----------------------------------------------------------

MARKET_WORDS = ("market", "remote")


def _market_for(ctx: ReplContext):
    """The pool's index, fetched now because a person asked. Raises `MarketError`."""
    from beeagent.plugins.catalog import MARKET_TIMEOUT, fetch_market

    config = getattr(ctx, "config", None)
    return fetch_market(str(getattr(config, "pool_url", "") or ""),
                        str(getattr(config, "pool_token", "") or ""),
                        MARKET_TIMEOUT)


def _market_line(market) -> str:
    """What the index is, in one line: which box, which licences, what was left out.

    Both numbers are printed because silence reads as "there was nothing to hide":
    the first is what the pool never published, the second is what this client
    additionally refused.
    """
    policy = ", ".join(market.policy) if market.policy else \
        L("the client's own allow list", "собственный список клиента")
    refused = L(f" · {market.excluded} refused here for their licence (not shown)",
                f" · {market.excluded} отклонено здесь по лицензии (не показано)") \
        if market.excluded else ""
    published = L(f" · the index itself excludes {market.published_excluded} entries",
                  f" · сам индекс не публикует {market.published_excluded} записей") \
        if market.published_excluded else ""
    return L(
        f"index from {market.host or 'the pool'} · licences accepted: {policy}"
        f" · {len(market.entries)} entries{refused}{published}",
        f"индекс с {market.host or 'пула'} · принимаемые лицензии: {policy}"
        f" · записей: {len(market.entries)}{refused}{published}")


def _market_table(market, installed: dict, title: str, query: str = "") -> Table:
    table = Table(title=bee_title(title), header_style="bold " + HONEY,
                  border_style=BORDER, box=box.SIMPLE_HEAVY)
    table.add_column("", width=2)
    table.add_column("name", style="bold #ffcc00")
    table.add_column("id", style="dim")
    table.add_column(L("description", "описание"))
    table.add_column(L("licence", "лицензия"), style="green")
    table.add_column(L("from (repo/path@commit)", "откуда (репо/путь@коммит)"), style="dim")
    table.add_column(L("status", "статус"), style="green")
    for entry in market.search(query):
        record = installed.get(entry.name)
        status = "" if record is None else (
            L("✅ installed", "✅ установлен") if record.get("enabled", True)
            else L("⏸ disabled", "⏸ выключен"))
        table.add_row(TYPE_ICON.get(entry.type, "•"), entry.name, entry.id,
                      entry.description, entry.license, entry.provenance, status)
    table.caption = Text(
        _market_line(market) + "\n" + L(
            "install: /plugin install <id> · the bytes come from the public commit, "
            "and only after their sha256 matches what the index promised",
            "установить: /plugin install <id> · байты приходят из публичного коммита, "
            "и только когда их sha256 совпадёт с обещанным в индексе"), style="dim")
    return table


def _cmd_market(ctx, args):
    """`/plugins market [filter]` — the pool's index, or the local catalog with a
    line saying why the market was not reached."""
    manager, _ = _extensions(ctx)
    query = " ".join(args)
    try:
        market = _market_for(ctx)
    except Exception as e:                       # a dead box is a listing, not a crash
        from rich.console import Group

        from beeagent.plugins.catalog import MarketError

        reason = str(e) if isinstance(e, MarketError) else \
            L(f"the market did not answer ({e.__class__.__name__})",
              f"рынок не ответил ({e.__class__.__name__})")
        local = _cmd_plugins(ctx, args)
        notice = Text(L(f"⚠ market not reached: {reason}",
                        f"⚠ рынок не отвечает: {reason}"), style="bold yellow")
        tail = Text(L("showing the catalog shipped with BeeCode instead.",
                      "показываю каталог, который идёт вместе с BeeCode."), style="dim")
        return CommandResult(output=Group(notice, local.output, tail))
    if not market.entries:
        return _err(L("the market answered, and every entry in it was refused by the "
                      "licence gate — nothing here is installable",
                      "рынок ответил, но лицензионный фильтр отклонил все записи — "
                      "ставить нечего"))
    entries = market.search(query)
    if not entries:
        return _err(L(f"nothing named “{query}” in the market index. /plugins market shows it all.",
                      f"в индексе рынка нет «{query}». /plugins market — весь список."))
    title = L(f"Market: {query}" if query else "Market: the pool's index",
              f"Рынок: {query}" if query else "Рынок: индекс пула")
    return CommandResult(output=_market_table(market, manager.installed(), title, query))


def _market_lookup(ctx: ReplContext, target: str):
    """One market entry for this name or id, or None.

    Returns (entry, complaint): a name two publishers share is a complaint, because
    answering with whichever came first installs the wrong thing silently.
    """
    market = _market_for(ctx)
    matches = market.find(target)
    if len(matches) > 1:
        return None, L(f"“{target}” is not unique in the market — ask by id: "
                       f"{', '.join(m.id for m in matches)}",
                       f"«{target}» в индексе рынка не уникально — проси по id: "
                       f"{', '.join(m.id for m in matches)}")
    return (matches[0] if matches else None), None


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
        return _err(f"usage: /plugin {sub} <name|git-url|market-id>")
    # --trust is a flag, not part of the name or the URL.
    trusted = "--trust" in rest
    target = " ".join(w for w in rest if w != "--trust")

    if sub == "install":
        # A git source is someone else's Python: the loader exec_module()s it on
        # the next start, so cloning it needs an explicit act of trust.
        if not target:
            return _err("usage: /plugin install <name|git-url|market-id> [--trust]")
        item = manager.catalog.get(target) or manager.catalog.find_git(target)
        if item is None:
            # Not shipped locally: the market is asked, and only now does this
            # command touch the network. A pool that sleeps is a readable line,
            # never a traceback and never a silent "not found".
            try:
                item, complaint = _market_lookup(ctx, target)
            except Exception as e:
                from beeagent.plugins.catalog import MarketError

                reason = str(e) if isinstance(e, MarketError) else \
                    L(f"the market did not answer ({e.__class__.__name__})",
                      f"рынок не ответил ({e.__class__.__name__})")
                return _err(L(f"could not install “{target}”: it is not in the shipped "
                              f"catalog, and the market was not reached — {reason}",
                              f"не удалось установить «{target}»: в каталоге BeeCode его нет, "
                              f"и рынок не отвечает — {reason}"))
            if complaint:
                return _err(complaint)
        if item is None:
            return _err(L(f"could not install “{target}”: it is in neither the shipped "
                          f"catalog nor the market index.",
                          f"не удалось установить «{target}»: его нет ни в каталоге BeeCode, "
                          f"ни в индексе рынка."))

        kind = item.source.get("kind")
        from beeagent.plugins.catalog import MarketItem

        entry = item if isinstance(item, MarketItem) else None
        # A market "plugin" or "mcp" entry is code from a public repo, same as a
        # git one: the licence covers copying it, nothing in it covers running it.
        if ((kind == "git") or (kind == "market" and item.type in ("plugin", "mcp"))) \
                and not trusted:
            return _err(L(f"“{target}” installs external code that runs inside BeeCode as tools. "
                          f"Read it first, then re-run: /plugin install {target} --trust",
                          f"«{target}» ставит чужой код, который исполняется внутри BeeCode "
                          f"как инструменты. Прочитай его, потом: /plugin install {target} --trust"))
        try:
            report = manager.install(target, item=item)
        except Exception as e:
            return _err(L(f"could not install “{target}”: {e}", f"не удалось установить «{target}»: {e}"))
        installed = (f"✅ installed {TYPE_ICON.get(report['type'], '')} {report['name']} "
                     f"({report['type']})")
        native = (f"✅ установлен {TYPE_ICON.get(report['type'], '')} {report['name']} "
                  f"({report['type']})")
        line = L(installed, native)
        if entry is not None:
            # Provenance over bytes: what was hashed, and where from.
            line += L(f"\n  licence {entry.license} · {entry.provenance}"
                      f"\n  sha256 {entry.sha256[:16]}… verified against the index",
                      f"\n  лицензия {entry.license} · {entry.provenance}"
                      f"\n  sha256 {entry.sha256[:16]}… совпал с обещанным в индексе")
        return _ok(line + _reload_extensions(ctx) + _wear_pack_skin(ctx, report["name"]))

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


def _cmd_stop(ctx, args):
    """Interrupt the answer being written. Ctrl+C does this too, but it also reads
    like the only way out, and on a phone there is no convenient Ctrl+C."""
    agent = ctx.agent
    was_running = bool(agent.request_stop()) if agent is not None else False
    return CommandResult(action="stop", output=Text(
        L("stopping after this step — the session stays open, /tasks shows what is left",
          "останавливаю после этого шага — сессия открыта, что осталось видно в /tasks")
        if was_running else
        L("nothing is running right now", "сейчас ничего не выполняется"), style="dim"))


def _cmd_tasks(ctx, args):
    """The agent's own task list, from the same file the `todo` tool writes."""
    import json
    from pathlib import Path

    from beeagent.tools.todo import TODO_FILE

    path = Path(TODO_FILE)
    if not path.exists():
        return CommandResult(output=Text(
            L("no task list yet — the agent keeps one with the todo tool",
              "списка задач пока нет — агент ведёт его инструментом todo"), style="dim"))
    try:
        tasks = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _err(L("the task list cannot be read", "список задач не читается"))
    if not isinstance(tasks, list) or not tasks:
        return CommandResult(output=Text(L("the task list is empty", "список задач пуст"),
                                         style="dim"))

    from rich.table import Table

    from beeagent.ui import skin
    from beeagent.ui.components import bee_title

    table = Table(title=bee_title(L("🐝 tasks", "🐝 задачи")),
                  **skin.frame_kwargs(BORDER), header_style="bold " + HONEY, expand=False)
    table.add_column("#", style="dim", width=4)
    table.add_column("state", width=8)
    table.add_column("task")
    left = 0
    for task in tasks:
        if not isinstance(task, dict):
            # One hand-edited line used to take the whole list down with an
            # AttributeError, so the command that reports the plan failed on it.
            left += 1
            table.add_row("?", "!", L("not a task record", "не запись задачи")
                          + f": {str(task)[:70]}")
            continue
        done = bool(task.get("done"))
        left += 0 if done else 1
        table.add_row(str(task.get("id", "?")),
                      "✔" if done else "…",
                      str(task.get("text", ""))[:90])
    table.caption = Text(L(f"{left} still open", f"открытых осталось: {left}"), style="dim")
    return CommandResult(output=table)


def _pool_provider_refresh(ctx) -> None:
    """Hand a freshly stored seat to the provider object that is already running.

    The Agent builds its providers once, from the config as it stood at startup.
    `/pool enroll` wrote the token into the config and the file, but the live
    PoolProvider kept the empty token it was constructed with -- so a person who
    had just taken a seat was told "no seat token yet, run /pool enroll" on every
    single message, and only a restart fixed it.
    """
    agent = getattr(ctx, "agent", None)
    if agent is None:
        return
    provider = agent.providers.get("pool")
    if provider is None:
        return
    provider.url = (ctx.config.pool_url or "").rstrip("/")
    provider.token = ctx.config.pool_token or ""


def _cmd_pool(ctx, args):
    """Address of the key pool, the seat this install holds, and its budget."""
    from beeagent.providers import pool as pool_mod

    sub = (args[0].lower() if args else "")
    rest = args[1:]
    config = ctx.config

    if sub == "url":
        url = (rest[0].strip() if rest else "")
        if not url.startswith(("http://", "https://")):
            return _err(L("give a full address: /pool url https://pool.example.com",
                          "нужен полный адрес: /pool url https://pool.example.com"))
        config.pool_url = url.rstrip("/")
        _persist_config(ctx)
        _pool_provider_refresh(ctx)
        note = "" if url.startswith("https://") else L(
            "\n  ⚠️ plain http — your seat token travels in the clear",
            "\n  ⚠️ простой http — токен места едет открытым текстом")
        return CommandResult(output=Text(
            L(f"pool address saved: {config.pool_url}. Now /pool enroll for a seat.{note}",
              f"адрес пула сохранён: {config.pool_url}. Теперь /pool enroll за местом.{note}"),
            style="#ffcc00"))

    if sub == "enroll":
        if not config.pool_url:
            return _err(L("no address yet: /pool url https://…", "сначала адрес: /pool url https://…"))
        try:
            body = pool_mod.enroll(config.pool_url)
        except Exception as e:
            return _err(L(f"the pool did not answer: {e}", f"пул не ответил: {e}"))
        token = str(body.get("token") or "")
        if not token:
            return _err(L("the pool gave no seat token", "пул не дал токен места"))
        config.pool_token = token
        _persist_config(ctx)
        _pool_provider_refresh(ctx)
        text = Text()
        text.append(L("🐝 seat taken. ", "🐝 место получено. ", ), style="bold #ffcc00")
        text.append(L(f"token …{token[-4:]} saved in beeagent.json — "
                      f"{body.get('requests_per_day', '?')} requests and "
                      f"{body.get('tokens_per_day', '?')} tokens a day.",
                      f"токен …{token[-4:]} сохранён в beeagent.json — "
                      f"{body.get('requests_per_day', '?')} запросов и "
                      f"{body.get('tokens_per_day', '?')} токенов в сутки."), style="dim")
        if not body.get("approved", True):
            text.append("\n" + L("the pool owner has to approve this seat before it answers.",
                                 "владелец пула должен подтвердить это место, иначе оно не работает."),
                        style="bold yellow")
        else:
            text.append("\n" + L("switch to it with: /provider pool",
                                 "переключись на него: /provider pool"), style="dim")
        return CommandResult(output=text)

    if sub in ("", "status"):
        text = Text()
        text.append(L("pool: ", "пул: ", ), style="dim")
        text.append(config.pool_url or L("not set — /pool url https://…",
                                         "не задан — /pool url https://…"), style="bold #ffcc00")
        text.append("\n" + L("seat: ", "место: ", ), style="dim")
        seat = config.pool_token or ""
        text.append(f"…{seat[-4:]}" if seat else L("none — /pool enroll", "нет — /pool enroll"),
                    style="bold #ffcc00")
        if config.pool_url:
            try:
                health = pool_mod.pool_status(config.pool_url, seat)
                text.append("\n" + L("server: ", "сервер: ", ), style="dim")
                text.append(json.dumps(health, ensure_ascii=False)[:160], style="bold #7cb342")
            except Exception as e:
                text.append("\n" + L("server: unreachable — ", "сервер не отвечает — ", ),
                            style="dim")
                text.append(str(e)[:120], style="bold yellow")
        return CommandResult(output=text)

    return _err(L("usage: /pool [url <address> | enroll | status]",
                  "использование: /pool [url <адрес> | enroll | status]"))


def _cmd_update(ctx, args):
    """Look at the published version now, and install it if it is newer."""
    from beeagent import __version__
    from beeagent.core import updater

    workdir = (getattr(ctx.agent, "workdir", None) or ".") if ctx.agent else "."
    body = updater.check(workdir=workdir, force=True)
    latest = str(body.get("latest") or "")
    if not latest:
        return _err(L("the update server did not answer — nothing was changed",
                      "сервер обновлений не ответил — ничего не тронуто"))
    if not updater.is_newer(latest, __version__):
        return CommandResult(output=Text(
            L(f"BeeCode {__version__} is the newest there is.",
              f"BeeCode {__version__} — самый свежий из вышедших."), style="dim"))
    return CommandResult(
        output=Text(L(f"found {latest} — updating", f"нашёл {latest} — обновляю"),
                    style="bold #ffcc00"),
        action="update")


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
    "update": _cmd_update,
    "pool": _cmd_pool,
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
    "stop": _cmd_stop,
    "tasks": _cmd_tasks,
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
    try:
        return handler(ctx, args)
    except Exception as e:
        # One command's bug must not take the conversation with it: the session
        # is only saved when the REPL loop ends, and a traceback ends it.
        return _err(L(f"/{name} failed: {e.__class__.__name__}: {e}",
                      f"/{name} упал: {e.__class__.__name__}: {e}"))


# `/trust` lives in core/trust.py because the gate and the command answer with the
# same words, but it is a core command: registered here, at import, so a plain
# `from beeagent.ui.commands import COMMANDS` sees the same registry the README is
# built from. It used to appear only once a PluginLoader happened to be built,
# which made the command list depend on import order.
from beeagent.core import compact as _compact  # noqa: E402  (bottom: it imports us lazily)
from beeagent.core import journal as _journal  # noqa: E402  (bottom: it imports us lazily)
from beeagent.core import skins as _skins  # noqa: E402  (bottom: it imports us lazily)
from beeagent.core import trust as _trust  # noqa: E402  (bottom: trust imports us lazily)

_trust.register_command()
_journal.register_command()
_compact.register_command()
_skins.register_command()
