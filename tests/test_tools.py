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

from beeagent.tools.edit import EditTool

def test_edit_replace(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("def hello():\n    print('world')\n")
    tool = EditTool()
    result = tool.execute(
        path=str(f),
        old_text="print('world')",
        new_text="print('hello')"
    )
    assert result.error is False
    assert "print('hello')" in f.read_text()

def test_edit_not_found(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("aaa")
    tool = EditTool()
    result = tool.execute(path=str(f), old_text="bbb", new_text="ccc")
    assert result.error is True

def test_edit_ambiguous(tmp_path):
    f = tmp_path / "code.py"
    f.write_text("aaa\naaa")
    tool = EditTool()
    result = tool.execute(path=str(f), old_text="aaa", new_text="bbb")
    assert result.error is True
    assert "Ambiguous" in result.output

def test_edit_is_not_safe():
    assert EditTool().is_safe() is False

from beeagent.tools.bash import BashTool

def test_bash_simple():
    tool = BashTool()
    result = tool.execute(command="echo hello")
    assert "hello" in result.output
    assert result.error is False

def test_bash_error():
    tool = BashTool()
    result = tool.execute(command="exit 1")
    assert result.error is True

def test_bash_is_not_safe():
    assert BashTool().is_safe() is False

def test_bash_metadata():
    tool = BashTool()
    result = tool.execute(command="echo test")
    assert result.metadata["returncode"] == 0

from beeagent.tools.grep import GrepTool
from beeagent.tools.glob_tool import GlobTool

def test_grep_search(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("def hello():\n    pass\ndef world():\n    pass\n")
    tool = GrepTool()
    result = tool.execute(pattern="def \\w+", path=str(tmp_path), include="*.py")
    assert "def hello" in result.output
    assert "def world" in result.output

def test_grep_no_matches(tmp_path):
    f = tmp_path / "test.py"
    f.write_text("hello world")
    tool = GrepTool()
    result = tool.execute(pattern="xyz", path=str(f))
    assert "No matches" in result.output

def test_grep_is_safe():
    assert GrepTool().is_safe() is True

def test_glob_find(tmp_path):
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.txt").write_text("")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "c.py").write_text("")
    tool = GlobTool()
    result = tool.execute(pattern="**/*.py", path=str(tmp_path))
    assert "a.py" in result.output
    assert "c.py" in result.output
    assert "b.txt" not in result.output

def test_glob_no_files(tmp_path):
    tool = GlobTool()
    result = tool.execute(pattern="*.xyz", path=str(tmp_path))
    assert "No files found" in result.output

def test_glob_is_safe():
    assert GlobTool().is_safe() is True

from beeagent.tools.web_search import WebSearchTool
from beeagent.tools.git import GitTool
from beeagent.tools.todo import TodoTool
from beeagent.tools.task import TaskTool

def test_web_search():
    tool = WebSearchTool()
    result = tool.execute(query="python tutorial")
    assert result.error is False
    assert len(result.output) > 0

def test_git_status():
    tool = GitTool()
    result = tool.execute(command="status")
    assert result.error is False

def test_todo_add_and_list(tmp_path, monkeypatch):
    import beeagent.tools.todo as todo_mod
    monkeypatch.setattr(todo_mod, "TODO_FILE", str(tmp_path / "todo.json"))
    tool = TodoTool()
    r1 = tool.execute(action="add", text="Buy milk")
    assert r1.error is False
    r2 = tool.execute(action="list")
    assert "Buy milk" in r2.output

def test_task_delegation():
    tool = TaskTool()
    result = tool.execute(description="Find all Python files")
    assert result.error is False
    assert "delegated" in result.metadata
