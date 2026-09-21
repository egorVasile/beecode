from pathlib import Path
from .base import BaseTool, ToolResult, read_text_preserving, write_text_preserving

class EditTool(BaseTool):
    name = "edit"
    description = (
        "Replace one exact, unique piece of text in a file. Read the file first and copy the text "
        "with its real indentation; when the match is ambiguous, include more surrounding lines. "
        "Prefer editing an existing file over creating a new one."
    )
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
            p = Path(str(path)).expanduser()
            if not p.exists():
                return ToolResult(output=f"File not found: {path}", error=True)
            content = read_text_preserving(p)
            find, replace = old_text, new_text
            if content.count(find) == 0 and "\r\n" in content:
                # The model read this file through splitlines(), which drops the
                # \r, so its fragment is LF-only while the file is CRLF. Match the
                # file's own endings rather than rewriting them.
                find = old_text.replace("\n", "\r\n")
                replace = new_text.replace("\n", "\r\n")
            count = content.count(find)
            if count == 0:
                return ToolResult(output=f"Text not found in {path}", error=True)
            if count > 1:
                return ToolResult(output=f"Ambiguous: {count} matches found", error=True)
            write_text_preserving(p, content.replace(find, replace, 1))
            return ToolResult(output=f"Replaced in {path}", error=False)
        except Exception as e:
            return ToolResult(output=str(e), error=True)
    
    def is_safe(self) -> bool:
        return False
