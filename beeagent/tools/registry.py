from typing import Optional
import difflib
import inspect

from .base import BaseTool

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
        # Extra names models invent for an existing tool ("read_directory" for
        # "list_directory"). Only `get()` accepts them, so the advertised
        # catalog keeps one canonical name per tool.
        self._aliases: dict[str, str] = {}

    def register(self, tool: BaseTool, replace: bool = False) -> bool:
        """Add a tool. A name that is taken is refused unless `replace` is said.

        Plugins and MCP servers arrive after the core tools. Letting one register
        `bash` or `write` would swap the implementation *and* inherit whatever
        grant the user gave the original — so a collision is refused, and the
        caller reports it. Replacing a tool on purpose is still possible, but it
        has to be written down.
        """
        existing = self._tools.get(tool.name)
        taken = existing is not None and existing is not tool and not replace
        # An alias is as owned as a name: `read_directory` reaches list_directory
        # through the alias table, so an extension registered under that name
        # would answer every call while /tools still showed only the real tool.
        alias_shadowed = self._aliases.get(tool.name) not in (None, tool.name)
        hijacked = [alias for alias in getattr(tool, "aliases", ())
                    if alias in self._tools and alias != tool.name]
        if taken or hijacked or alias_shadowed:
            return False
        self._tools[tool.name] = tool
        for alias in getattr(tool, "aliases", ()):
            if alias not in self._tools:
                self._aliases[alias] = tool.name
        return True

    def unregister(self, name: str) -> bool:
        self._aliases = {a: t for a, t in self._aliases.items() if t != name}
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Optional[BaseTool]:
        tool = self._tools.get(name)
        if tool is not None:
            return tool
        target = self._aliases.get(name)
        return self._tools.get(target) if target else None

    def canonical_name(self, name: str) -> Optional[str]:
        """Registered name behind `name`, resolving aliases (not typos)."""
        if name in self._tools:
            return name
        return self._aliases.get(name)

    def resolve(self, name: str) -> Optional[str]:
        """Closest registered tool name, to survive model typos.

        Only very similar names are accepted — a wrong guess would run a tool
        with arguments meant for another one.
        """
        matches = difflib.get_close_matches(name, self.list_names(), n=1, cutoff=0.8)
        return matches[0] if matches else None

    def list_names(self) -> list[str]:
        return list(self._tools.keys())

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def to_schemas(self) -> list[dict]:
        return [t.to_schema() for t in self._tools.values()]
