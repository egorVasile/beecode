from dataclasses import dataclass, field

@dataclass
class ToolResult:
    output: str
    error: bool
    metadata: dict = field(default_factory=dict)

class BaseTool:
    name: str = ""
    description: str = ""
    parameters: dict = field(default_factory=dict)
    
    def execute(self, **kwargs) -> ToolResult:
        raise NotImplementedError
    
    def is_safe(self) -> bool:
        return False
    
    def to_schema(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }
