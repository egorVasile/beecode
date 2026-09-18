from beeagent.core.agent import Agent
from beeagent.core.parser import CommandParser
from beeagent.config.schema import BeeConfig

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
    assert agent.tools.get("task") is not None

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
