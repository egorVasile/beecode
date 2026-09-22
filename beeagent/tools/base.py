import os
from dataclasses import dataclass, field
from inspect import signature


@dataclass
class ToolResult:
    output: str
    error: bool
    metadata: dict = field(default_factory=dict)


def read_text_preserving(path) -> str:
    """UTF-8 text with the file's own line endings intact.

    Both defaults fight us here. Universal newlines turn CRLF into LF on read and
    write `os.linesep` back on save, so every file the agent touches arrives as a
    whole-file diff; and `errors="replace"` reads a cp1251 file as replacement
    characters that the model then writes back as if they were its content.
    """
    with open(path, "r", encoding="utf-8", newline="") as handle:
        return handle.read()


def write_text_preserving(path, text: str) -> int:
    """Write UTF-8 text verbatim through a temporary file; byte count, not char count.

    `open(path, "w")` truncates the target before a single byte is verified, so a
    Ctrl+C, a full disk or a killed worker thread turned "edit this file" into
    "delete this file". Writing beside it and renaming is the only sequence where
    the original is still there if we fail.
    """
    target = str(path)
    tmp = target + ".beecode-tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as handle:
            written = handle.write(text)
            handle.flush()
        size = len(text.encode("utf-8")) if written is None else written
        os.replace(tmp, target)
        return size
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

class BaseTool:
    name: str = ""
    description: str = ""
    parameters: dict = {}
    # Names models invent for this tool; resolved by the registry, never advertised.
    aliases: tuple[str, ...] = ()
    # Set for anything a plugin or MCP server provided: its own `is_safe()` is a
    # claim we do not trust, and permissions treat it as unsafe until granted.
    from_extension: bool = False
    # Whether running this tool leaves a byte changed on disk. `is_safe()` means
    # "only reads"; this one means "even a tool that is safe to look with writes",
    # which is what /permissions readonly has to refuse.
    writes_files: bool = False

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

        Only those generic names are poured in by position. A call that arrives
        with *meaningful* keys we do not recognise — `find`/`replace` for an
        edit — must not be mapped onto `old_text`/`new_text` by dict order: that
        can swap the search text with the replacement and rewrite a file with
        what the model never asked for.
        """
        if self._open_kwargs():
            return args
        params = self.params()
        generic = ("input", "arg", "value", "text")
        known = {k: v for k, v in args.items() if k in params}
        spare = [(k, v) for k, v in args.items()
                 if k not in params and k.startswith(generic)]
        for name in params:
            if spare and name not in known:
                known[name] = spare.pop(0)[1]
        return known

    def missing_args(self, args: dict) -> list[str]:
        """Required parameters still absent after coercion."""
        required = self.parameters.get("required")
        if required is None:
            if self._open_kwargs():
                return []       # an **kwargs tool takes whatever it is given
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
