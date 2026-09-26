"""Load installed plugins/skills/MCP servers into a running Agent.

- plugin packs (plugin.py with TOOLS=[...]) -> registered as agent tools
- skills (SKILL.md) -> listed in the system prompt, full text via `skill` tool
- MCP servers (mcp.json) -> each server tool becomes mcp_<server>_<tool>,
  schemas discovered once and cached on disk (npx downloads are slow)

The first two are text the folder supplied; the third is a command it supplied.
None of them is applied from a folder the user has not agreed to — see
`beeagent/core/trust.py`, and `withheld` below for what was skipped and why.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from beeagent.plugins.manager import PluginManager
from beeagent.plugins.mcp import McpManager
from beeagent.tools.base import BaseTool, ToolResult


@dataclass
class Skill:
    name: str
    description: str
    path: Path

    def content(self) -> str:
        return self.path.read_text(encoding="utf-8", errors="replace")

    def body(self) -> str:
        """Instructions without the YAML header (for human rendering)."""
        return strip_frontmatter(self.content())


class SkillTool(BaseTool):
    name = "skill"
    description = (
        "Load the full instructions of an installed skill. First call `skill` "
        "with action=list to see available skills, then action=load with a name."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "load"],
                       "description": "list skills or load one"},
            "name": {"type": "string", "description": "Skill name (for action=load)"},
        },
        "required": ["action"],
    }

    def __init__(self, loader: "PluginLoader"):
        self.loader = loader

    def execute(self, action: str = "list", name: str = "") -> ToolResult:
        skills = self.loader.skills
        if action == "list":
            if not skills:
                return ToolResult(
                    output="No skills installed. /plugins to browse the catalog.",
                    error=False)
            lines = [f"{s.name}: {s.description}" for s in skills]
            return ToolResult(output="\n".join(lines), error=False)
        if action == "load":
            for s in skills:
                if s.name == name:
                    return ToolResult(output=s.content(), error=False)
            return ToolResult(
                output=f"Skill '{name}' not found. Use action=list first.", error=True)
        return ToolResult(output=f"Unknown action '{action}' (list|load).", error=True)

    def is_safe(self) -> bool:
        return True


class McpDynamicTool(BaseTool):
    """One MCP tool exposed as an agent tool (runs over its own thread loop)."""

    def __init__(self, mcp: McpManager, server: str, tool_schema: dict):
        safe = re.sub(r"\W+", "_", f"mcp_{server}_{tool_schema.get('name', 'tool')}").strip("_")
        self.name = safe
        self.mcp = mcp
        self.server = server
        self.tool = tool_schema.get("name", "tool")
        self.description = f"[{server}] {tool_schema.get('description', '')}".strip()
        self.parameters = tool_schema.get("inputSchema", {"type": "object", "properties": {}})

    def execute(self, **kwargs) -> ToolResult:
        out = self.mcp.call_blocking(self.server, self.tool, kwargs)
        return ToolResult(output=out, error=out.startswith("(MCP"))

    # No is_safe() override: a tool from a third-party MCP server is code we do
    # not audit, and `is_safe: True` here let it run in `readonly` mode and under
    # a grant the user gave to a *different* tool. Grant it by name: /allow <tool>.


class PluginLoader:
    """Wires installed extensions into one Agent instance.

    `gate_project=True` is the startup path — `Agent()` passing it says "these
    extensions came with the folder I was started in", and a folder that came from
    somewhere else does not get to run its own Python or register its own servers
    until the user agreed to that folder (core/trust.py). A loader built by hand is
    an embedding program vouching for the bytes, so it is not gated.
    """

    def __init__(self, agent, gate_project: bool = False):
        self.agent = agent
        self.manager = PluginManager()
        self.mcp = McpManager()
        from beeagent.core import trust

        self.trust = trust
        workdir = self.manager.project_root()
        self.gate = (trust.for_folder(workdir) if gate_project
                     else trust.unenforced_gate(workdir))
        self.skills: list[Skill] = []
        self.load_errors: list[str] = []
        # Extensions the folder asked for and BeeCode did NOT run, in words the
        # user reads: a skipped plugin is never reported as an active one.
        self.withheld: list[str] = []
        # Names of the tools this loader added, so a reload can remove them.
        self.tool_names: list[str] = []
        # Configured servers whose schemas are not cached yet.
        self.pending_mcp: list[str] = []
        # What each plugin contributed through the extension API.
        from beeagent.ext.api import ExtensionRegistry

        self.extensions = ExtensionRegistry()

    # --- entry point -----------------------------------------------------------

    def load_all(self) -> None:
        # The interface slots are set from the config before anything draws, so a
        # user who switched the frame off never sees the default one first.
        from beeagent.ui import skin

        skin.apply(getattr(self.agent.config, "ui", None) or {})
        self._load_skill_tool()
        self._load_plugins()
        self._load_skills()
        self._load_mcp_servers()
        self._wear_skin()
        self._announce()

    def _wear_skin(self) -> None:
        """Switch to the skin the user chose, once the packs have registered it.

        After the plugins, because a skin is usually contributed by a pack: switched
        to before them, the name would not exist yet and the choice would look like
        a broken setting. A name that is still unknown is said once, quietly — the
        skin may belong to a pack he has not installed in this folder.
        """
        from beeagent.core import skins

        wanted = str(getattr(self.agent.config, "skin", "") or "").strip()
        if not wanted or wanted == skins.BASELINE:
            return
        reason = skins.switch(wanted)
        if reason:
            # The same list `/extensions` prints for a pack that failed: a setting
            # that quietly did nothing is the thing people then go reinstalling.
            self.load_errors.append(reason)

    def _announce(self) -> None:
        """Register the one command that answers for this folder, then ask.

        Both interfaces go through `commands.dispatch`, and this runs before
        either of them owns the screen: the classic REPL and the TUI build their
        UIs after `Agent()`, so this is the moment a question can be put — and the
        moment `/trust` has to exist for the sidebar and the palette to offer it.
        """
        if not self.gate.enforced:
            return
        from beeagent.core import trust

        try:
            trust.register_command()
        except Exception:
            # No command is not a reason to load code nobody agreed to; the
            # withholding already happened and stays in effect.
            pass
        try:
            trust_decision = self.gate.announce()
        except Exception:
            trust_decision = None
        if trust_decision == trust.TRUSTED:
            # Answered at the prompt: the folder's own code can come in now. Only
            # the two gated passes run again — a `reset()` here would unregister
            # the `skill` tool that is already answering the model.
            self._load_plugins()
            self._load_mcp_servers()
            # ...and the gate the folder asked for, which `load_config` had held
            # back: a "y" here means the same thing `/trust yes` means.
            for line in trust.apply_grant(self.agent, self.gate):
                trust.report(line)

    def reset(self) -> None:
        """Drop every tool this loader registered (before a re-scan)."""
        for name in self.tool_names:
            self.agent.tools.unregister(name)
        self.tool_names.clear()
        self.skills.clear()
        self.load_errors.clear()
        self.withheld.clear()
        self.pending_mcp.clear()
        self.extensions.clear()

    def _register(self, tool: BaseTool, from_extension: bool = False) -> bool:
        """Register one tool, remembering only the ones that actually landed.

        A plugin that is refused (its name is taken) must not be listed for
        removal later, or a reload would unregister the core tool it collided
        with and leave the session unable to run `bash` at all.
        """
        tool.from_extension = from_extension
        if not self.agent.tools.register(tool):
            self.load_errors.append(
                f"{tool.name}: a tool with this name is already registered — "
                "rename it or remove the other one")
            return False
        self.tool_names.append(tool.name)
        return True

    # --- pieces ------------------------------------------------------------------

    def _load_skill_tool(self) -> None:
        self._register(SkillTool(self))

    def _load_plugins(self) -> None:
        from beeagent.ext.api import ExtensionAPI

        for plugin_dir in self.manager.installed_plugin_dirs():
            entry = plugin_dir / "plugin.py"
            digest = self.trust.digest_file(entry)
            if not self.gate.allow("plugin", plugin_dir.name, digest):
                # Someone else's `exec_module` is a change to how BeeCode behaves,
                # and this folder has not been agreed to: not imported, not run,
                # and said out loud (see `_announce`).
                self._withhold("plugin", plugin_dir.name, entry)
                continue
            module_name = f"beeagent_plugin_{plugin_dir.name}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, entry)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                tools = getattr(module, "TOOLS", [])
                for tool in tools:
                    if self._register(tool, from_extension=True):
                        self.extensions.add("tool", tool.name, plugin_dir.name,
                                            (tool.description or "").split("\n")[0][:60])
                # The newer contract: a plugin that wants to add commands,
                # settings or listeners declares setup(api) and never touches a
                # private module of ours.
                setup = getattr(module, "setup", None)
                if callable(setup):
                    setup(ExtensionAPI(plugin_dir.name, self.extensions,
                                       agent=self.agent, config=self.agent.config))
            except Exception as e:
                self.load_errors.append(f"plugin '{plugin_dir.name}': {e}")

    def _withhold(self, kind: str, name: str, where) -> None:
        """Record one extension that was not applied, for the question and `/trust`."""
        line = self.trust.withheld_line(kind, name, where)
        if line not in self.withheld:
            self.withheld.append(line)
        self.gate.withhold(line)

    def _load_skills(self) -> None:
        for skill_dir in self.manager.installed_skill_dirs():
            path = skill_dir / "SKILL.md"
            try:
                skill = Skill(
                    name=skill_dir.name,
                    description=_frontmatter_field(path, "description") or f"skill from {skill_dir.name}",
                    path=path,
                )
                self.skills.append(skill)
            except Exception as e:
                self.load_errors.append(f"skill '{skill_dir.name}': {e}")

    def _load_mcp_servers(self) -> None:
        """Register only servers whose schemas are already cached.

        First-time discovery can take a minute (npx downloads), so it never
        runs on startup — /mcp connect does it explicitly.
        """
        for server, cfg in self.manager.mcp_servers().items():
            if not cfg.get("enabled", True):
                continue
            digest = self.manager.mcp_digest(server, cfg)
            if not self.gate.allow("mcp", server, digest):
                # `mcp.json` names a command BeeCode would spawn: same rule as a
                # plugin.py, because the folder wrote it, not the user.
                self._withhold("mcp", server, self.manager.mcp_path)
                continue
            tools = self.mcp.cached_tools(server)
            if tools is None:
                self.pending_mcp.append(server)
                continue
            self._register_mcp_tools(server, tools)

    def _register_mcp_tools(self, server: str, tools: list[dict]) -> None:
        for schema in tools:
            self._register(McpDynamicTool(self.mcp, server, schema), from_extension=True)

    def connect_mcp(self, server: str, timeout: float = 90.0) -> list[dict]:
        """Discover a server's tools now, cache them, and register the tools."""
        tools = self.mcp.discover_tools_blocking(server, timeout=timeout)
        self._register_mcp_tools(server, tools)
        if server in self.pending_mcp:
            self.pending_mcp.remove(server)
        return tools

    # --- prompt section --------------------------------------------------------

    def skills_prompt_section(self) -> str:
        """A compact block for the system prompt; details load via `skill`."""
        if not self.skills:
            return ""
        lines = [
            "# SKILLS",
            "You have expert skill documents installed. To use one, call the "
            "`skill` tool (action=load, name=...) and follow its instructions:",
        ]
        for s in self.skills:
            lines.append(f"- {s.name}: {s.description}")
        return "\n".join(lines)

    def shutdown(self) -> None:
        self.mcp.shutdown()


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def _frontmatter_field(path: Path, field: str) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    if not text.startswith("---"):
        return ""
    end = text.find("\n---", 3)
    if end == -1:
        return ""
    for line in text[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            if key.strip() == field:
                return value.strip()
    return ""
