"""BeeCode extension system: skills, plugins, and MCP servers.

Inspired by the "everything is a plugin" architecture: the agent core stays
small, and every capability (a skill document, a Python tool pack, an MCP
server) is an installable unit from a catalog.
"""
from beeagent.plugins.manager import PluginManager, PLUGINS_DIR, STATE_PATH, MCP_CONFIG_PATH
from beeagent.plugins.catalog import Catalog, CatalogItem, TYPE_ICON
from beeagent.plugins.mcp import McpManager

__all__ = [
    "PluginManager", "Catalog", "CatalogItem", "TYPE_ICON",
    "McpManager", "PLUGINS_DIR", "STATE_PATH", "MCP_CONFIG_PATH",
]
