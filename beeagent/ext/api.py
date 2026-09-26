"""The stable surface a plugin is allowed to touch.

An extension should be able to add a slash command, a tool for the model, a
setting of its own, a listener for agent events and a variant of the interface —
without importing the REPL, patching a module or knowing how the next version of
BeeCode is laid out. Everything a plugin can reach goes through this object, so
the rest of the code stays free to move.

What a plugin gets, and what it does not: it may *add*, never *replace*. A tool
or command whose name is already taken is refused rather than silently winning
the argument — an extension that could take over `bash` would inherit whatever
permission the user gave to the real one.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from beeagent.i18n import L


@dataclass
class Contribution:
    """One thing a plugin added, so `/extensions` can say who is responsible."""
    kind: str          # command | tool | setting | event | skin
    name: str
    plugin: str
    note: str = ""


@dataclass
class ExtensionRegistry:
    """Everything every plugin contributed, in the order it arrived."""
    contributions: list[Contribution] = field(default_factory=list)
    commands: dict = field(default_factory=dict)          # name -> handler(ctx, args)
    listeners: dict = field(default_factory=dict)         # event -> [callback]
    settings: dict = field(default_factory=dict)          # "plugin.key" -> (default, help)

    def add(self, kind: str, name: str, plugin: str, note: str = "") -> None:
        self.contributions.append(Contribution(kind=kind, name=name, plugin=plugin, note=note))

    def by_plugin(self, plugin: str) -> list[Contribution]:
        return [c for c in self.contributions if c.plugin == plugin]

    def clear(self) -> None:
        """Forget everything plugins contributed, so a reload starts from nothing.

        Reloading runs every plugin's setup() a second time. Without this the
        same listener fires twice per event, /extensions repeats every row, and
        a plugin collides with the command it registered on the previous pass.
        """
        from beeagent.ui import commands as core

        for contribution in self.contributions:
            if contribution.kind == "command":
                core.drop_command(contribution.name)
        self.contributions.clear()
        self.commands.clear()
        self.listeners.clear()
        self.settings.clear()

    def setting_keys(self, plugin: str) -> list[str]:
        return sorted(k for k in self.settings if k.startswith(plugin + "."))


class ExtensionAPI:
    """The object handed to a plugin's `setup(api)`."""

    def __init__(self, plugin: str, registry: ExtensionRegistry, agent=None, config=None):
        self.plugin = plugin
        self.registry = registry
        self.agent = agent
        self.config = config

    # --- commands -----------------------------------------------------------

    def command(self, name: str, description: str, handler, usage: str = "") -> bool:
        """Add a slash command. Refused only against a command BeeCode owns."""
        from beeagent.ui import commands as core

        clean = name.lstrip("/")
        existing = next((c for c in core.COMMANDS if c.name == clean), None)
        if existing is not None and existing.category != "plugins":
            # Recorded under its own kind: `clear()` removes the commands an
            # extension added, and a refusal is not one of those — filed here as
            # "command" it would delete the core command on the next reload.
            self.registry.add("command-refused", clean, self.plugin, L("refused: name taken",
                                                                       "отказ: имя занято"))
            return False
        # A command another extension registered earlier is replaced, not
        # refused: a second Agent in this process loads the same plugins again,
        # and "name taken" would leave it without /undo or /doctor.
        core.drop_command(clean)
        core.add_command(clean, description, usage=usage)
        self.registry.commands[clean] = handler
        core.HANDLERS[clean] = _wrap(handler)
        self.registry.add("command", clean, self.plugin, description)
        return True

    # --- tools --------------------------------------------------------------

    def tool(self, tool) -> bool:
        """Give the model a new capability. Refused if it would shadow a tool."""
        if self.agent is None:
            self.registry.add("tool", getattr(tool, "name", "?"), self.plugin,
                              L("refused: no agent", "отказ: нет агента"))
            return False
        tool.from_extension = True
        if not self.agent.tools.register(tool):
            self.registry.add("tool", tool.name, self.plugin,
                              L("refused: name taken", "отказ: имя занято"))
            return False
        self.registry.add("tool", tool.name, self.plugin,
                          (tool.description or "").split("\n")[0][:60])
        return True

    # --- settings -----------------------------------------------------------

    def setting(self, key: str, default, description: str = ""):
        """Declare a setting of our own. The user overrides it in beeagent.json:

            "extensions": { "<plugin>": { "<key>": <value> } }
        """
        full = f"{self.plugin}.{key}"
        self.registry.settings[full] = (default, description)
        self.registry.add("setting", full, self.plugin, description)
        return default

    def get(self, key: str, default=None):
        """Read our setting, with the user's override applied."""
        stored = getattr(self.config, "extensions", None) or {}
        mine = stored.get(self.plugin, {}) if isinstance(stored, dict) else {}
        if key in mine:
            return mine[key]
        declared = self.registry.settings.get(f"{self.plugin}.{key}")
        return mine.get(key, declared[0] if declared else default)

    # --- events -------------------------------------------------------------

    def event(self, name: str, callback) -> None:
        """Watch what the agent does: stream_delta, tool_start, tool_end, done."""
        self.registry.listeners.setdefault(name, []).append(callback)
        self.registry.add("event", name, self.plugin, getattr(callback, "__name__", "callback"))

    # --- interface ----------------------------------------------------------

    def skin(self, slot: str, name: str, value) -> None:
        """Offer a variant of the interface: a frame style, a spinner, a renderer."""
        from beeagent.ui import skin

        skin.register(slot, name, value)
        self.registry.add("skin", f"{slot}:{name}", self.plugin)

    def set_skin(self, slot: str, name: str) -> bool:
        """Choose an existing variant — allowed, but the user's own choice wins later."""
        from beeagent.ui import skin

        return skin.choose(slot, name, source=f"plugin:{self.plugin}")

    def skin_hooks(self, name: str, hooks: dict, description: str = "") -> bool:
        """Register a programmable skin: the lifecycle, not a slot.

        `skin()` offers one *piece* of the interface to the legacy slot table; this
        hands the whole `on_init`/`on_frame`/`on_event`/surface set to the skin
        host, which is what a pack that animates the screen needs. True when the
        host took it.

        The folder name is recorded as the skin's other handle: a user installs
        `skin-pulse` off the shelf and types what he installed, so `/skins
        skin-pulse` has to reach the skin that calls itself `pulse`.
        """
        from beeagent.core import skins

        try:
            entry = skins.register(name, hooks, description=description,
                                   pack=self.plugin)
        except Exception:
            return False
        if not entry.refused:
            # Said out loud in `/extensions`, the way a slot variant is: a pack that
            # owns a whole skin has to be visible next to the packs that own a spinner.
            self.registry.add("skin", f"{name} (hooks)", self.plugin)
        return not entry.refused


def _wrap(handler):
    """Adapt `handler(ctx, args)` to the command contract used by the REPL."""
    from beeagent.ui.commands import CommandResult, _err
    from beeagent.i18n import L as _L

    def inner(ctx, args):
        try:
            result = handler(ctx, list(args))
        except Exception as e:                       # a plugin bug must not kill the REPL
            return _err(_L(f"plugin command failed: {e}", f"команда плагина упала: {e}"))
        if isinstance(result, CommandResult):
            return result
        if result is None:
            return CommandResult(output=None)
        return CommandResult(output=str(result))

    return inner


def emit(registry: ExtensionRegistry, event: str, data: dict) -> None:
    """Hand an agent event to every listener a plugin registered."""
    for callback in registry.listeners.get(event, ()):
        try:
            callback(event, data)
        except Exception:
            # A listener that throws must not interrupt the answer it watched.
            pass
