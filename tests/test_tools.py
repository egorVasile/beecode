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

from beeagent.tools.read import ReadTool

def test_read_file(tmp_path):
    f = tmp_path / "test.txt"
    f.write_text("hello world")
    tool = ReadTool()
    result = tool.execute(path=str(f))
    assert result.output == "1: hello world"
    assert result.error is False

def test_read_missing_file():
    tool = ReadTool()
    result = tool.execute(path="/nonexistent/file.txt")
    assert result.error is True

def test_read_is_safe():
    assert ReadTool().is_safe() is True

def test_read_with_offset_limit(tmp_path):
    f = tmp_path / "lines.txt"
    f.write_text("line1\nline2\nline3\nline4\nline5")
    tool = ReadTool()
    result = tool.execute(path=str(f), offset=1, limit=2)
    assert "2: line2" in result.output
    assert "3: line3" in result.output
    assert "1: line1" not in result.output

def test_read_metadata(tmp_path):
    f = tmp_path / "meta.txt"
    f.write_text("a\nb\nc")
    tool = ReadTool()
    result = tool.execute(path=str(f))
    assert result.metadata["total_lines"] == 3

from beeagent.tools.write import WriteTool

def test_write_file(tmp_path):
    f = tmp_path / "output.txt"
    tool = WriteTool()
    result = tool.execute(path=str(f), content="hello world")
    assert result.error is False
    assert f.read_text() == "hello world"

def test_write_creates_dirs(tmp_path):
    f = tmp_path / "sub" / "dir" / "file.txt"
    tool = WriteTool()
    result = tool.execute(path=str(f), content="nested")
    assert result.error is False
    assert f.read_text() == "nested"

def test_write_is_not_safe():
    assert WriteTool().is_safe() is False
