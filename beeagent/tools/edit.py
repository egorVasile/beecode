from pathlib import Path
from .base import BaseTool, ToolResult

class EditTool(BaseTool):
    name = "edit"
    description = "Replace exact text in a file"
    parameters = {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "File path"},
            "old_text": {"type": "string", "description": "Exact text to find"},
            "new_text": {"type": "string", "description": "Replacement text"},
        },
        "required": ["path", "old_text", "new_text"],
    }
    
    def execute(self, path: str, old_text: str, new_text: str) -> ToolResult:
        try:
            p = Path(path)
            if not p.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            content = p.read_text(encoding="utf-8")
            count = content.count(old_text)
            if count == 0:
                return ToolResult(output=f"Text not found in {path}", error=True)
            if count > 1:
                return ToolResult(output=f"Ambiguous: {count} matches found", error=True)
            new_content = content.replace(old_text, new_text, 1)
            p.write_text(new_content, encoding="utf-8")
            return ToolResult(output=f"Replaced in {path}", error=False)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False
