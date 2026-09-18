from beeagent.core.agent import Agent
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
    config = BeeConfig()
    agent = Agent(config=config)
    
    # Single tool call
    response = '{"tool": "read", "args": {"path": "test.py"}}'
    calls = agent._parse_tool_calls(response)
    assert len(calls) == 1
    assert calls[0]["tool"] == "read"
    
    # Multiple tool calls
    response = '{"tool": "read", "args": {"path": "a.py"}}\n{"tool": "bash", "args": {"command": "ls"}}'
    calls = agent._parse_tool_calls(response)
    assert len(calls) == 2
    
    # No tool calls (plain text)
    response = "This is a normal response."
    calls = agent._parse_tool_calls(response)
    assert len(calls) == 0

def test_parse_tool_calls_json_block():
    config = BeeConfig()
    agent = Agent(config=config)
    
    response = '```json\n{"tool": "read", "args": {"path": "test.py"}}\n```'
    calls = agent._parse_tool_calls(response)
    assert len(calls) == 1
    assert calls[0]["tool"] == "read"