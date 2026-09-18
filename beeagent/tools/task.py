from .base import BaseTool, ToolResult

class TaskTool(BaseTool):
    name = "task"
    description = "Delegate a sub-task to a separate agent context"
    parameters = {
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Task description"},
        },
        "required": ["description"],
    }
    
    def execute(self, description: str) -> ToolResult:
        return ToolResult(
            output=f"Sub-task delegated: {description}\n(Result will be available when agent loop is connected)",
            error=False,
            metadata={"delegated": True, "description": description},
        )
    
    def is_safe(self) -> bool:
        return True