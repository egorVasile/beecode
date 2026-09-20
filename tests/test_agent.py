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

    async def fake_stream(provider, messages, callback=None, model=""):
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

    async def fake_stream(provider, messages, callback=None, model=""):
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


def test_a_silent_endpoint_times_out_and_recovers(monkeypatch):
    """The hang the user saw: stream opens, one token arrives, then silence."""
    import asyncio
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session

    class Stalling:
        name = "stalling"

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            yield ("content", "начал")
            if self.calls == 1:
                await asyncio.sleep(3600)      # never resumes

        async def chat(self, messages, model=""):
            return "готово"

    agent = Agent(config=BeeConfig(stream_idle_timeout=3))
    endpoint = Stalling()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    events = []

    answer = asyncio.run(agent.run(
        "сделай", session=Session(),
        callback=lambda e, d: events.append(e)))

    assert answer == "готово", "the turn must end, not hang on a silent stream"
    assert endpoint.calls == 1        # one stream, then the same attempt falls back
    assert "stream_reset" in events, "the half-typed fragment is cleared for the UI"


def test_the_wait_is_announced_then_gives_up(monkeypatch):
    """Heartbeat: silence is reported while waiting is still worth it."""
    import asyncio
    import beeagent.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "HEARTBEAT_SECONDS", 0.2)
    agent = Agent(config=BeeConfig())

    async def stalling():
        await asyncio.sleep(5)
        yield ("content", "x")

    announced = []

    async def go():
        try:
            await agent._next_token(stalling().__aiter__(), 1,
                                    lambda event, data: announced.append(data["seconds"]), True)
        except asyncio.TimeoutError:
            return "timed out"
        return "returned"

    assert asyncio.run(go()) == "timed out"
    assert announced and max(announced) <= 1


def test_a_slow_stream_is_not_truncated_by_the_heartbeat(monkeypatch):
    """Polling silence must not cancel the generator between chunks."""
    import asyncio
    import beeagent.core.agent as agent_mod

    monkeypatch.setattr(agent_mod, "HEARTBEAT_SECONDS", 0.1)
    agent = Agent(config=BeeConfig())

    async def slow():
        yield ("content", "при")
        await asyncio.sleep(0.4)          # several heartbeat slices, still alive
        yield ("content", "вет")

    async def go():
        iterator = slow().__aiter__()
        pieces = []
        while True:
            try:
                kind, text = await agent._next_token(iterator, 5, None, False)
            except StopAsyncIteration:
                break
            pieces.append(text)
        return "".join(pieces)

    assert asyncio.run(go()) == "привет"


def test_prompt_echo_is_not_treated_as_an_answer():
    """OpenaiChat guest mode answers with a copy of our own prompt."""
    import asyncio
    from beeagent.core.session import Session

    class Echoing:
        name = "echoing"

        def __init__(self):
            self.calls = 0

        async def chat_stream(self, messages, model=""):
            self.calls += 1
            if self.calls == 1:
                yield ("content", "OpenaiChat: Guest prompt: [tool result] "
                                  "[SYSTEM: You are BeeCode, an autonomous coding agent")
            else:
                yield ("content", "всё работает")

        async def chat(self, messages, model=""):
            return "всё работает"

    agent = Agent(config=BeeConfig())
    endpoint = Echoing()
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    session = Session()

    answer = asyncio.run(agent.run("проверь", session=session))

    assert answer == "всё работает"
    stored = " ".join(m.content for m in session.messages)
    assert "[SYSTEM: You are" not in stored, "the echo must not poison history"


def test_echo_detector_covers_verbatim_repeats_and_rejects_nothing_short():
    agent = Agent(config=BeeConfig())
    request = ("сделай задачу и проверь тестами результат работы агента, "
               "а потом покажи короткий отчёт о том, что именно поменялось")
    sent = [{"role": "user", "content": request}]
    assert agent._is_prompt_echo(request, sent)
    assert not agent._is_prompt_echo("готово", sent)
    assert not agent._is_prompt_echo(
        "совсем другой ответ, которого в исходном запросе точно никогда не было", sent)
