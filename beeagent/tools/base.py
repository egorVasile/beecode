from dataclasses import dataclass, field
from inspect import signature


@dataclass
class ToolResult:
    output: str
    error: bool
    metadata: dict = field(default_factory=dict)

class BaseTool:
    name: str = ""
    description: str = ""
    parameters: dict = {}
    # Names models invent for this tool; resolved by the registry, never advertised.
    aliases: tuple[str, ...] = ()

    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError

    def _open_kwargs(self) -> bool:
        """True when execute() takes **kwargs (MCP tools, plugin packs)."""
        params = signature(self.execute).parameters.values()
        return any(p.kind is p.VAR_KEYWORD for p in params)

    def params(self) -> list[str]:
        """Parameter names `execute()` accepts, for error messages."""
        if self._open_kwargs():
            return list(self.parameters.get("properties", {}))
        return [name for name in signature(self.execute).parameters if name != "self"]

    def coerce_args(self, args: dict) -> dict:
        """Fit loosely-parsed arguments onto the real parameters.

        The tag-style fallback in the parser produces keys like `input`/`arg1`
        that no tool declares; without this they die as a TypeError and the
        model just repeats the same call.
        """
        if self._open_kwargs():
            return args
        params = self.params()
        known = {k: v for k, v in args.items() if k in params}
        spare = [v for k, v in args.items() if k not in params]
        for name in params:
            if spare and name not in known:
                known[name] = spare.pop(0)
        return known

    def missing_args(self, args: dict) -> list[str]:
        """Required parameters still absent after coercion."""
        required = self.parameters.get("required")
        if required is None:
            params = signature(self.execute).parameters
            required = [n for n, p in params.items() if n != "self" and p.default is p.empty]
        return [name for name in required if name not in args]

    def is_safe(self) -> bool:
        return False

    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
