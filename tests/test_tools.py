from beeagent.tools.base import BaseTool, ToolResult
from beeagent.tools.registry import ToolRegistry

class DummyTool(BaseTool):
    name = "dummy"
    description = "A dummy tool"
    parameters = {"type": "object", "properties": {"msg": {"type": "string"}}}
    
    def execute(self, msg: str = "hello") -> ToolResult:
        return ToolResult(output=f"echo: {msg}", error=False)
    
    def is_safe(self) -> bool:
        return True

def test_tool_result():
    r = ToolResult(output="ok", error=False)
    assert r.error is False
    assert r.output == "ok"

def test_registry_discover():
    registry = ToolRegistry()
    registry.register(DummyTool())
    assert "dummy" in registry.list_names()
    assert registry.get("dummy") is not None

def test_registry_get_missing():
    registry = ToolRegistry()
    assert registry.get("nonexistent") is None

def test_tool_to_schema():
    tool = DummyTool()
    schema = tool.to_schema()
    assert schema["name"] == "dummy"
    assert schema["description"] == "A dummy tool"
    assert "parameters" in schema

def test_registry_schemas():
    registry = ToolRegistry()
    registry.register(DummyTool())
    schemas = registry.to_schemas()
    assert len(schemas) == 1
    assert schemas[0]["name"] == "dummy"

def test_registry_list_tools():
    registry = ToolRegistry()
    registry.register(DummyTool())
    tools = registry.list_tools()
    assert len(tools) == 1
    assert tools[0].name == "dummy"
