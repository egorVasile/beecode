import json
from pathlib import Path

from beeagent.tools.base import BaseTool, ToolResult


class JsonFormatTool(BaseTool):
    name = "json_format"
    description = (
        "Validate JSON and pretty-print it. Pass either `text` (a JSON string) "
        "or `path` (a file to read). Returns formatted JSON or a precise error."
    )
    parameters = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "JSON string to format"},
            "path": {"type": "string", "description": "Path to a JSON file"},
            "indent": {"type": "integer", "description": "Indent width", "default": 2},
            "sort_keys": {"type": "boolean", "description": "Sort object keys", "default": False},
        },
    }

    def execute(self, text: str = "", path: str = "", indent: int = 2, sort_keys: bool = False) -> ToolResult:
        if not text and not path:
            return ToolResult(output="Pass `text` or `path`.", error=True)
        try:
            if path:
                text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
            return ToolResult(
                output=json.dumps(data, indent=indent, sort_keys=sort_keys, ensure_ascii=False),
                error=False,
            )
        except json.JSONDecodeError as e:
            return ToolResult(
                output=f"Invalid JSON at line {e.lineno}, column {e.colno}: {e.msg}", error=True
            )
        except OSError as e:
            return ToolResult(output=str(e), error=True)

    def is_safe(self) -> bool:
        return True


TOOLS = [JsonFormatTool()]
