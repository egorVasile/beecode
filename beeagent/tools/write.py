from pathlib import Path
from .base import BaseTool, ToolResult, write_text_preserving

class WriteTool(BaseTool):
    name = "write"
    description = (
        "Create a new file or rewrite one completely (missing parent directories are created). "
        "Use edit for changes to existing code, and do not create files the task does not need — "
        "no notes, summaries or logs unless someone asked for them."
    )
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path to write"},
            "content": {"type": "string", "description": "Content to write"},
        },
        "required": ["path", "content"],
    }
    
    def execute(self, path: str, content: str) -> ToolResult:
        try:
            if not isinstance(path, str) or not path.strip():
                return ToolResult(output="`path` must be a non-empty string", error=True)
            p = Path(path).expanduser()
            if p.exists():
                refusal = _not_text_yet(p)
                if refusal:
                    return ToolResult(output=refusal, error=True)
            p.parent.mkdir(parents=True, exist_ok=True)
            written = write_text_preserving(p, content)
            return ToolResult(output=f"Written {written} bytes to {path}", error=False)
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
        return (f"{p} is UTF-16 — writing UTF-8 here would break it. Convert it "
                "on purpose, not as a side effect of an edit")
    try:
        with open(p, "rb") as handle:
            handle.read(64 * 1024).decode("utf-8")
    except UnicodeDecodeError:
        return (f"{p} is not UTF-8 text — read it with bash if you must, and do not "
                "write it back as text")
    return ""
