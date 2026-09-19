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
from beeagent.tools.list_dir import ListDirectoryTool

def test_web_search():
    tool = WebSearchTool()
    result = tool.execute(query="python tutorial")
    assert result.error is False
    assert len(result.output) > 0

def test_git_status():
    import os
    tool = GitTool()
    old_cwd = os.getcwd()
    os.chdir("C:\\agent")
    try:
        result = tool.execute(command="status")
        assert result.error is False
    finally:
        os.chdir(old_cwd)

def test_todo_add_and_list(tmp_path, monkeypatch):
    import beeagent.tools.todo as todo_mod
    monkeypatch.setattr(todo_mod, "TODO_FILE", str(tmp_path / "todo.json"))
    tool = TodoTool()
    r1 = tool.execute(action="add", text="Buy milk")
    assert r1.error is False
    r2 = tool.execute(action="list")
    assert "Buy milk" in r2.output

def test_todo_add_needs_text_and_ids_must_exist(tmp_path, monkeypatch):
    import beeagent.tools.todo as todo_mod
    monkeypatch.setattr(todo_mod, "TODO_FILE", str(tmp_path / "todo.json"))
    tool = TodoTool()
    assert tool.execute(action="add").error is True          # no silent empty task
    assert tool.execute(action="done", id=99).error is True  # no fake success
    assert tool.execute(action="remove", id=99).error is True
    assert tool.execute(action="nope").error is True
    # a corrupt list file must not raise out of the tool
    (tmp_path / "todo.json").write_text("{not json", encoding="utf-8")
    assert tool.execute(action="list").output == "No tasks"


def test_list_directory_shows_dirs_and_sizes(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.py").write_text("print(1)")
    result = ListDirectoryTool().execute(str(tmp_path))
    assert result.error is False
    lines = result.output.split("\n")
    assert any(l == "sub/" for l in lines)
    assert any(l.startswith("a.py") and l.endswith("B") for l in lines)


def test_list_directory_recursive_and_errors(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("x")
    deep = ListDirectoryTool().execute(str(tmp_path), recursive=True)
    assert "sub/b.txt" in deep.output
    file_arg = ListDirectoryTool().execute(str(tmp_path / "sub" / "b.txt"))
    assert file_arg.error is True and "read tool" in file_arg.output
    assert ListDirectoryTool().execute(str(tmp_path / "missing")).error is True


def test_read_on_a_directory_points_at_list_directory(tmp_path):
    from beeagent.tools.read import ReadTool
    result = ReadTool().execute(str(tmp_path))
    assert result.error is True
    assert "list_directory" in result.output


def test_tools_report_truncation(tmp_path):
    from beeagent.tools.glob_tool import GlobTool
    for i in range(120):
        (tmp_path / f"f{i:03d}.py").write_text("x")
    out = GlobTool().execute("*.py", str(tmp_path)).output
    assert "more files not shown" in out


def test_loose_args_are_coerced_onto_parameters():
    from beeagent.tools.read import ReadTool
    from beeagent.tools.grep import GrepTool
    # the tag-style parser emits input/arg1 keys no tool declares
    assert ReadTool().coerce_args({"input": "a.py"}) == {"path": "a.py"}
    assert ReadTool().missing_args(ReadTool().coerce_args({"input": "a.py"})) == []
    assert GrepTool().coerce_args({"arg1": "def x", "arg2": "."}) == {
        "pattern": "def x", "path": "."}
    assert GrepTool().missing_args({"pattern": "x"}) == ["path"]


def test_open_kwargs_tools_pass_arguments_through():
    from beeagent.tools.base import BaseTool

    class Passthrough(BaseTool):
        name = "passthrough"
        parameters = {"type": "object", "properties": {"q": {"type": "string"}},
                      "required": ["q"]}

        def execute(self, **kwargs):
            return ToolResult(output=str(kwargs), error=False)

    tool = Passthrough()
    assert tool.coerce_args({"q": "hello"}) == {"q": "hello"}
    assert tool.params() == ["q"]
    assert tool.missing_args({}) == ["q"]


def test_registry_resolves_typos_only(capsys):
    from beeagent.tools.registry import ToolRegistry
    from beeagent.tools.read import ReadTool

    reg = ToolRegistry()
    reg.register(ReadTool())
    assert reg.resolve("reads") == "read"
    assert reg.resolve("reaid") == "read"
    assert reg.resolve("reed") is None      # too far away to guess
    # an invented tool with a plausible meaning must NOT be silently remapped
    assert reg.resolve("read_directory") is None
    assert reg.resolve("web_search") is None
