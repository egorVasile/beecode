"""`write` — the tool that can replace a whole file in one call."""
import codecs
import os

from beeagent.i18n import L

from ._path_policy import guard
from .base import BaseTool, ToolResult, write_text_preserving

# A 64 KiB probe split a multibyte character and answered "not UTF-8" about a
# perfectly valid file, so an incremental decoder carries the partial character
# across the chunk boundary instead of failing on it.
_PROBE_CHUNK = 64 * 1024
_PROBE_LIMIT = 8 * 1024 * 1024


class WriteTool(BaseTool):
    name = "write"
    description = (
        "Create a new file or rewrite one completely (missing parent directories are created). "
        "Use edit for changes to existing code, and do not create files the task does not need — "
        "no notes, summaries or logs unless someone asked for them. Only inside the working "
        "directory: a path outside it is refused, never silently redirected."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write (inside the working directory)"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }

    def execute(self, path: str, content: str) -> ToolResult:
        try:
            if not isinstance(path, str) or not path.strip():
                return ToolResult(output=L("`path` must be a non-empty string",
                                           "`path` должен быть непустой строкой"), error=True)
            if not isinstance(content, str):
                return ToolResult(
                    output=L("`content` must be text, not " + type(content).__name__,
                             "`content` должен быть текстом, а не " + type(content).__name__),
                    error=True)
            # Checked before anything is created: `../../x` used to build the
            # directories on its way to writing the file.
            target, refusal = guard(path, "write")
            if refusal:
                return ToolResult(output=refusal, error=True,
                                  metadata={"refused": "outside-working-directory"})
            if target.exists():
                refusal = _not_text_yet(target)
                if refusal:
                    return ToolResult(output=refusal, error=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            write_text_preserving(target, content)
            # The count is measured from the disk, not from len(content): the
            # number the model repeats back to the user has to be the file's.
            try:
                written = os.path.getsize(target)
            except OSError:
                written = len(content.encode("utf-8"))
            return ToolResult(
                output=L(f"Written {written} bytes to {path}", f"Записано {written} байт в {path}"),
                error=False, metadata={"bytes": written, "resolved": str(target)})
        except Exception as e:
            return ToolResult(output=str(e), error=True)

    def is_safe(self) -> bool:
        return False


def _not_text_yet(p) -> str:
    """Refuse to turn a UTF-16 or binary file into UTF-8 by writing over it.

    `read` already refuses these, but a model can still be handed the contents by
    a tool result and try to write them back — and then a .vcxproj or a
    PowerShell script becomes unreadable to the program that owns it.
    """
    try:
        head = p.read_bytes()[:4]
    except OSError:
        return ""
    if head[:2] in (bytes([255, 254]), bytes([254, 255])) or head[:4] == bytes([0, 0, 254, 255]):
        return L(f"{p} is UTF-16 — writing UTF-8 here would break it. Convert it "
                 f"on purpose, not as a side effect of an edit",
                 f"{p} — UTF-16: записать сюда UTF-8 значит его сломать. Конвертировать "
                 f"нужно намеренно, а не по ходу правки")
    try:
        _decode_probe(p)
    except UnicodeDecodeError as e:
        return L(f"{p} is not UTF-8 text ({e.reason} at byte {e.start}) — read it with bash "
                 f"if you must, and do not write it back as text",
                 f"{p} — не текст в UTF-8 ({e.reason} на байте {e.start}): при необходимости "
                 f"читай его через bash и не пиши его обратно как текст")
    return ""


def _decode_probe(p) -> None:
    """Raise UnicodeDecodeError if the file is not UTF-8 text.

    Feeding whole chunks to `bytes.decode` made a valid file fail whenever a
    Cyrillic character happened to straddle the 64 KiB edge; the incremental
    decoder holds the trailing half of it until the next chunk arrives.  The
    probe stops early and is never finalised, because a file longer than the
    budget only has to be UTF-8 *up to here* to be text.
    """
    decoder = codecs.getincrementaldecoder("utf-8")()
    remaining = _PROBE_LIMIT
    with open(p, "rb") as handle:
        while remaining > 0:
            chunk = handle.read(min(_PROBE_CHUNK, remaining))
            if not chunk:
                decoder.decode(b"", True)       # a file that ends mid-character is not text
                return
            remaining -= len(chunk)
            decoder.decode(chunk)
