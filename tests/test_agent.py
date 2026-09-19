from beeagent.core.agent import Agent
from beeagent.core.parser import CommandParser
from beeagent.core.session import Session
from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session

def test_agent_init():
    config = BeeConfig()
    agent = Agent(config=config)
    assert agent.config.model == "gpt-4"
    assert agent.tools.get("read") is not None
    assert agent.tools.get("write") is not None
    assert agent.tools.get("edit") is not None
    assert agent.tools.get("bash") is not None
    assert agent.tools.get("grep") is not None
    assert agent.tools.get("glob") is not None
    assert agent.tools.get("web_search") is not None
    assert agent.tools.get("git") is not None
    assert agent.tools.get("todo") is not None
    assert agent.tools.get("list_directory") is not None
    assert agent.tools.get("read_directory") is agent.tools.get("list_directory")
    assert "task" not in agent.tools.list_names()

def test_agent_providers():
    config = BeeConfig()
    agent = Agent(config=config)
    assert "g4f" in agent.providers.list_names()

def test_agent_economy():
    config = BeeConfig(mode="economy")
    agent = Agent(config=config)
    assert agent.economy.should_cache() is True

def test_parse_tool_calls():
    parser = CommandParser()

    # Single tool call
    response = '{"tool": "read", "args": {"path": "test.py"}}'
    parsed = parser.parse(response)
    assert parsed.has_commands is True
    assert len(parsed.commands) == 1
    assert parsed.commands[0].tool == "read"

    # Multiple tool calls
    response = '{"tool": "read", "args": {"path": "a.py"}}\n{"tool": "bash", "args": {"command": "ls"}}'
    parsed = parser.parse(response)
    assert parsed.has_commands is True
    assert len(parsed.commands) == 2

    # No tool calls (plain text)
    response = "This is a normal response."
    parsed = parser.parse(response)
    assert parsed.has_commands is False
    assert len(parsed.commands) == 0

def test_parse_tool_calls_json_block():
    parser = CommandParser()

    response = '```json\n{"tool": "read", "args": {"path": "test.py"}}\n```'
    parsed = parser.parse(response)
    assert parsed.has_commands is True
    assert len(parsed.commands) == 1
    assert parsed.commands[0].tool == "read"

def test_parse_mixed_response():
    parser = CommandParser()

    response = 'I will read the file for you.\n```json\n{"tool": "read", "args": {"path": "main.py"}}\n```\nLet me check the output.'
    parsed = parser.parse(response)
    assert parsed.has_commands is True
    assert len(parsed.commands) == 1
    assert "I will read" in parsed.text or "read the file" in parsed.text

def test_format_tool_prompt():
    parser = CommandParser()
    tools = [
        {"name": "read", "description": "Read a file", "parameters": {"type": "object", "properties": {"path": {"type": "string", "description": "File path"}}, "required": ["path"]}},
    ]
    prompt = parser.format_tool_prompt(tools)
    assert "read" in prompt
    assert "json" in prompt.lower()
    assert "tool" in prompt.lower()


def test_windows_paths_in_tool_calls_are_parsed():
    """Models write single backslashes into JSON, which is invalid escaping."""
    parser = CommandParser()
    raw = r'{"tool": "bash", "args": {"command": "ls C:\Users\Админ\proj"}}'
    parsed = parser.parse(raw)
    assert parsed.has_commands
    assert parsed.commands[0].args["command"] == r"ls C:\Users\Админ\proj"

    fenced = parser.parse(r'```json' + "\n" + r'{"tool": "read", "args": {"path": "D:\proj\sub.py"}}' + "\n" + r'```')
    assert fenced.has_commands and fenced.commands[0].args["path"] == r"D:\proj\sub.py"

    # real JSON escapes must survive untouched
    ok = parser.parse('{"tool": "write", "args": {"path": "a.txt", "content": "line\\n"}}')
    assert ok.commands[0].args["content"] == "line\n"


def test_advertised_tools_all_exist():
    """The catalog is generated from the registry, so every name in the prompt
    must be callable — and the invented names must still resolve."""
    import re
    from beeagent.core.context import ContextManager

    agent = Agent(config=BeeConfig())
    schemas = agent.tools.to_schemas()
    prompt = ContextManager(model="x").build_messages([], schemas)[0]["content"]
    params = {p for s in schemas for p in s["parameters"].get("properties", {})}
    for name in re.findall(r"`([a-z_]+)`", prompt):
        # every identifier the prompt points at with backticks is a real tool
        # or a real parameter of one
        assert agent.tools.get(name) is not None or name in params, name
    assert "read_directory" not in prompt          # aliases are not advertised
    assert agent.tools.get("read_directory") is agent.tools.get("list_directory")


def test_alias_call_runs_the_canonical_tool(tmp_path, monkeypatch):
    import asyncio
    agent = Agent(config=BeeConfig())
    (tmp_path / "sub").mkdir()
    events = []

    calls = iter(['{"tool": "list_files", "args": {"path": "%s"}}' % tmp_path.as_posix(), "готово"])

    async def fake_stream(provider, messages, callback=None):
        text = next(calls)
        if callback:
            callback("stream_delta", {"text": text})
        return text

    monkeypatch.setattr(agent, "_stream_response", fake_stream)
    answer = asyncio.run(agent.run("покажи файлы", session=Session(),
                                   callback=lambda e, d: events.append((e, d))))

    renamed = [d for e, d in events if e == "tool_renamed"]
    assert renamed == [{"from": "list_files", "to": "list_directory"}]
    started = [d for e, d in events if e == "tool_start"]
    assert started and started[0]["tool"] == "list_directory"
    assert "sub/" in [d for e, d in events if e == "tool_end"][0]["output"]
    assert answer == "готово"


def test_missing_arguments_are_reported_to_the_model(monkeypatch):
    import asyncio
    agent = Agent(config=BeeConfig())
    events = []
    calls = iter(['{"tool": "grep", "args": {"pattern": "def x"}}', "всё"])

    async def fake_stream(provider, messages, callback=None):
        text = next(calls)
        if callback:
            callback("stream_delta", {"text": text})
        return text

    monkeypatch.setattr(agent, "_stream_response", fake_stream)
    asyncio.run(agent.run("найди", session=Session(),
                          callback=lambda e, d: events.append((e, d))))

    errors = [d for e, d in events if e == "tool_error"]
    assert errors and "path" in errors[0]["message"]
    assert not [d for e, d in events if e == "tool_start"], "a call with missing args must not run"
