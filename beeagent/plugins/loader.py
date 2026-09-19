"""Load installed plugins/skills/MCP servers into a running Agent.

- plugin packs (plugin.py with TOOLS=[...]) -> registered as agent tools
- skills (SKILL.md) -> listed in the system prompt, full text via `skill` tool
- MCP servers (mcp.json) -> each server tool becomes mcp_<server>_<tool>,
  schemas discovered once and cached on disk (npx downloads are slow)
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

    def is_safe(self) -> bool:
        return True


class PluginLoader:
    """Wires installed extensions into one Agent instance."""

    def __init__(self, agent):
        self.agent = agent
        self.manager = PluginManager()
        self.mcp = McpManager()
        self.skills: list[Skill] = []
        self.load_errors: list[str] = []
        # Names of the tools this loader added, so a reload can remove them.
        self.tool_names: list[str] = []
        # Configured servers whose schemas are not cached yet.
        self.pending_mcp: list[str] = []

    # --- entry point -----------------------------------------------------------

    def load_all(self) -> None:
        self._load_skill_tool()
        self._load_plugins()
        self._load_skills()
        self._load_mcp_servers()

    def reset(self) -> None:
        """Drop every tool this loader registered (before a re-scan)."""
        for name in self.tool_names:
            self.agent.tools.unregister(name)
        self.tool_names.clear()
        self.skills.clear()
        self.load_errors.clear()
        self.pending_mcp.clear()

    def _register(self, tool: BaseTool) -> None:
        self.agent.tools.register(tool)
        self.tool_names.append(tool.name)

    # --- pieces ------------------------------------------------------------------

    def _load_skill_tool(self) -> None:
        self._register(SkillTool(self))

    def _load_plugins(self) -> None:
        for plugin_dir in self.manager.installed_plugin_dirs():
            entry = plugin_dir / "plugin.py"
            module_name = f"beeagent_plugin_{plugin_dir.name}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, entry)
                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                tools = getattr(module, "TOOLS", [])
                for tool in tools:
                    self._register(tool)
            except Exception as e:
                self.load_errors.append(f"plugin '{plugin_dir.name}': {e}")

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
            tools = self.mcp.cached_tools(server)
            if tools is None:
                self.pending_mcp.append(server)
                continue
            self._register_mcp_tools(server, tools)

    def _register_mcp_tools(self, server: str, tools: list[dict]) -> None:
        for schema in tools:
            self._register(McpDynamicTool(self.mcp, server, schema))

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
