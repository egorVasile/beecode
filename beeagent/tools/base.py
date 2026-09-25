import os
from dataclasses import dataclass, field
from inspect import signature

from beeagent.i18n import L


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


def reject_symlink_target(target: str) -> str:
    """Stop a write that would replace a symlink with a plain file.

    `os.replace` renames over the *link itself*, not over what it points at. So a
    "write config.json" on a link into a shared directory would delete that link,
    leave a regular file in its place and leave the real file holding the old
    bytes — while the tool announced a clean write. The user then edits a file
    that nothing else reads, and every other path still linked to the shared one
    disagrees with it. Nothing in the result says the shape of the tree changed.

    Following the link instead was considered and rejected: `write` on a name
    inside the project would then rewrite a file the permission check never saw
    (the diagram tool's linked SVG is exactly this escape), and an atomic rename
    onto the real file needs a temporary inside a directory the caller never
    named and may not own. Refusing costs one turn and names the path to use;
    either success would be a surprise written to disk.

    A caller that wants the link followed — `write` resolves its path before it
    arrives here, and confines the result to the working directory — hands over a
    name that is not a link, so this stays a backstop for every other user of the
    helper: `todo`, plugins, scripts and whatever calls it next.

    Returns the path unchanged when there is nothing to refuse, and raises OSError
    naming the real file when the target is a link.
    """
    if not os.path.islink(target):
        return target
    real = os.path.realpath(target)
    raise OSError(L(
        f"{target} is a symlink to {real}. Writing it would delete the link and "
        f"leave a plain file behind, so {real} would keep the old bytes while the "
        f"result claimed otherwise. Write to {real} instead if the shared file "
        "really is the target — and expect that to affect everything linked to it.",
        f"{target} — символьная ссылка на {real}. Запись удалит саму ссылку и "
        f"оставит обычный файл: {real} сохранил бы старые байты, а результат "
        f"заявил бы об обратном. Если нужен именно общий файл, пишите напрямую в "
        f"{real} — и помните, что на него указывают и другие пути."
    ))


def write_text_preserving(path, text: str) -> int:
    """Write UTF-8 text verbatim through a temporary file; the real byte count.

    `open(path, "w")` truncates the target before a single byte is verified, so a
    Ctrl+C, a full disk or a killed worker thread turned "edit this file" into
    "delete this file". Writing beside it and renaming is the only sequence where
    the original is still there if we fail.

    The size handed back is measured from the bytes on disk, because the number
    the model reasons about has to be the number the file really has: a text-mode
    `handle.write()` returns *characters*, and this function reported that as
    bytes, so the `write` tool announced "Written 13 bytes" for a 25-byte
    Cyrillic file.

    Raises OSError for a symlinked target (see `reject_symlink_target`) and for a
    short write; the target keeps its old bytes in both cases.
    """
    target = str(path)
    tmp = target + ".beecode-tmp"
    # Encode before anything is created. Text mode writes characters, so a lone
    # surrogate used to fail halfway and leave half a file on disk — the very
    # truncate-before-validate this function exists to avoid.
    payload = text.encode("utf-8")
    try:
        reject_symlink_target(target)
        # Binary mode: no newline translation to fight the caller's `newline=""`,
        # and `written` counts bytes instead of pretending characters are bytes.
        with open(tmp, "wb") as handle:
            written = handle.write(payload)
            handle.flush()
        on_disk = os.path.getsize(tmp)
        if written != len(payload) or on_disk != len(payload):
            # A full disk can report a short write without raising. Renaming the
            # truncated temp file over the target would lose both copies.
            raise OSError(L(
                f"incomplete write: {on_disk} of {len(payload)} bytes reached the disk",
                f"неполная запись: на диске {on_disk} байт из {len(payload)}"))
        os.replace(tmp, target)
        return on_disk
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
    # Set by the loop before execute(): True when the user granted this tool for
    # the session or runs in `auto`. A tool that can do both a harmless and a
    # config-loading kind of work uses it to pick the harmless one alone.
    granted: bool = False
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
