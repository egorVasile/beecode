from typing import Optional
from .base import BaseTool

class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, BaseTool] = {}
    
    def register(self, tool: BaseTool):
        self._tools[tool.name] = tool
    
    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)
    
    def list_names(self) -> list[str]:
        return list(self._tools.keys())
    
    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())
    
    def to_schemas(self) -> list[dict]:
        return [t.to_schema() for t in self._tools.values()]
