"""The permission gate, secret hygiene, and the model/provider match.

These cover behaviour the loop used to get wrong silently: running a tool the
user never allowed, replaying a cached tool call as an answer, and sending
"gpt-4" to an endpoint that has never heard of it.
"""
import asyncio

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.permissions import Permissions
from beeagent.core.session import Session
from beeagent.providers.presets import BY_NAME
from beeagent.tools.base import BaseTool, ToolResult
from beeagent.tools.read import ReadTool
from beeagent.ui.commands import ReplContext, _redact_secrets, dispatch


class Recorder(BaseTool):
    """An unsafe tool that records whether the loop actually ran it."""

    name = "recorder"
    description = "test tool that changes something"
    parameters = {"type": "object", "properties": {"note": {"type": "string"}},
                  "required": ["note"]}

    def __init__(self, safe=False):
        self.calls: list[str] = []
        self._safe = safe

    def execute(self, note: str) -> ToolResult:
        self.calls.append(note)
        return ToolResult(output="done", error=False)

    def is_safe(self) -> bool:
        return self._safe


def _agent(config=None, **kwargs):
    agent = Agent(config=config or BeeConfig(**kwargs))
    spy = Recorder()
    agent.tools.register(spy)
    agent.spy = spy
    return agent


def _run(agent, replies):
    """Feed the loop canned model answers and collect the UI events."""
    calls = iter(replies)
    events = []

    async def fake_stream(provider, messages, callback=None, model=""):
        text = next(calls)
        if callback:
            callback("stream_delta", {"text": text})
        return text

    agent._stream_response = fake_stream
    answer = agent.run_sync("do it", session=Session(),
                            callback=lambda e, d: events.append((e, d)))
    return answer, events


def _ctx(agent=None, config=None):
    config = config or (agent.config if agent is not None else BeeConfig())
    return ReplContext(agent=agent, config=config, session=Session())


def _text(renderable) -> str:
    from rich.console import Console

    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


# --- the gate in the loop --------------------------------------------------

def test_unsafe_tool_waits_for_a_grant():
    agent = _agent()                      # default permissions.mode == "ask"
    _, events = _run(agent, ['{"tool": "recorder", "args": {"note": "rm"}}', "done"])

    assert agent.spy.calls == []
    denied = [d for e, d in events if e == "tool_denied"]
    assert denied and denied[0]["tool"] == "recorder"
    assert any(e == "done" for e, _ in events), "the loop must keep going"


def test_granted_tool_runs():
    agent = _agent()
    agent.permissions.grant("recorder")
    _, events = _run(agent, ['{"tool": "recorder", "args": {"note": "rm"}}', "done"])

    assert agent.spy.calls == ["rm"]
    assert not [e for e, _ in events if e == "tool_denied"]


def test_reading_tools_need_no_grant():
    agent = _agent()
    agent.tools.register(Recorder(safe=True), replace=True)
    quiet = agent.tools.get("recorder")
    _, events = _run(agent, ['{"tool": "recorder", "args": {"note": "peek"}}', "done"])
    assert quiet.calls == ["peek"]
    assert not [e for e, _ in events if e == "tool_denied"]


def test_auto_mode_runs_everything():
    agent = _agent(config=BeeConfig(permissions={"mode": "auto"}))
    _run(agent, ['{"tool": "recorder", "args": {"note": "go"}}', "done"])
    assert agent.spy.calls == ["go"]


def test_readonly_mode_ignores_grants():
    agent = _agent(config=BeeConfig(permissions={"mode": "readonly",
                                                 "allowed": ["recorder"]}))
    _run(agent, ['{"tool": "recorder", "args": {"note": "no"}}', "done"])
    assert agent.spy.calls == []


def test_a_refusal_is_explained_to_the_model():
    agent = _agent()
    session = Session()
    _run_with_session(agent, ['{"tool": "recorder", "args": {"note": "x"}}', "done"], session)
    tool_msgs = [m.content for m in session.messages if m.role == "tool"]
    assert any("/allow recorder" in m for m in tool_msgs)


def _run_with_session(agent, replies, session):
    calls = iter(replies)

    async def fake_stream(provider, messages, callback=None, model=""):
        return next(calls)

    agent._stream_response = fake_stream
    answer = agent.run_sync("do it", session=session)
    return answer, []


def test_second_refusal_tells_the_model_to_stop_asking():
    spy = Recorder()
    perms = Permissions(mode="ask")
    perms.denied_this_run.add(spy.name)
    assert "already refused" in perms.refusal(spy).lower()


# --- permissions unit ------------------------------------------------------

def test_modes_decide():
    spy = Recorder()
    assert Permissions(mode="auto").allows(spy)
    assert not Permissions(mode="ask").allows(spy)
    assert Permissions(mode="ask", allowed=["recorder"]).allows(spy)
    assert not Permissions(mode="readonly", allowed=["recorder"]).allows(spy)
    assert Permissions(mode="readonly").allows(ReadTool())


def test_unknown_mode_falls_back_to_ask():
    assert Permissions(mode="yolo").mode == "ask"


def test_prompt_section_lists_what_is_blocked():
    agent = _agent()
    section = agent.permissions.prompt_section(agent.tools)
    assert "bash" in section and "Not granted" in section
    assert Permissions(mode="auto").prompt_section(agent.tools) == ""


def test_grant_survives_a_config_save(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    agent.permissions.grant("bash")
    agent.sync_config_permissions()
    assert agent.config.permissions.allowed == ["bash"]


# --- commands --------------------------------------------------------------

def test_allow_command_grants_and_revokes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    ctx = _ctx(agent)
    out = dispatch(ctx, "/allow recorder").output.plain
    assert "recorder" in out and agent.permissions.granted == {"recorder"}

    revoked = dispatch(ctx, "/allow remove recorder").output.plain
    assert "recorder" in revoked and agent.permissions.granted == set()
    assert "never granted" in dispatch(ctx, "/allow remove recorder").output.plain


def test_allow_rejects_a_typo_and_a_tool_that_needs_nothing():
    agent = _agent()
    ctx = _ctx(agent)
    assert "no tool" in dispatch(ctx, "/allow recorde").output.plain
    assert "only reads" in dispatch(ctx, "/allow read").output.plain


def test_permissions_command_switches_mode(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _agent()
    ctx = _ctx(agent)
    assert "ask" in _text(dispatch(ctx, "/permissions").output)      # the table names the modes
    result = dispatch(ctx, "/permissions auto")
    assert agent.permissions.mode == "auto"
    assert agent.config.permissions.mode == "auto"
    assert "auto" in result.output.plain
    assert "nonsense" in dispatch(ctx, "/permissions nonsense").output.plain


def test_config_never_prints_a_token():
    config = BeeConfig(api_keys={"groq": "gsk_supersecret_1234"},
                       custom_providers=[{"name": "x", "type": "ollama",
                                          "url": "http://h", "model": "m", "key": "sk-abcdef9999"}])
    rows = _redact_secrets(config.model_dump())
    assert "gsk_supersecret_1234" not in str(rows)
    assert "sk-abcdef9999" not in str(rows)
    assert "1234" in str(rows) and "9999" in str(rows)      # the tail still identifies it


def test_bare_key_command_reports_instead_of_deleting(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config = BeeConfig(api_keys={"groq": "gsk_keepme_4321"})
    ctx = _ctx(_agent(config=config), config)
    out = dispatch(ctx, "/key groq").output.plain
    assert config.api_keys["groq"] == "gsk_keepme_4321"
    assert "gsk_keepme_4321" not in out and "4321" in out


def test_git_plugin_install_demands_trust(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = _ctx(_agent())
    out = dispatch(ctx, "/plugin install https://example.com/evil-plugin.git").output.plain
    assert "--trust" in out
    assert not (tmp_path / ".beeagent" / "plugins" / "evil-plugin").exists()


# --- provider / model match ------------------------------------------------

def test_model_follows_the_selected_provider():
    config = BeeConfig(provider="groq", api_keys={"groq": "gsk_x"}, model="gpt-4")
    agent = Agent(config=config)
    _, events = _run(agent, ["готово"])
    assert config.model in BY_NAME["groq"].models
    assert agent.context.model == config.model
    switched = [d for e, d in events if e == "model_switched"]
    assert switched and switched[0]["from"] == "gpt-4"


def test_g4f_keeps_a_model_it_advertises():
    config = BeeConfig(provider="g4f", model="deepseek-r1")
    agent = Agent(config=config)
    _run(agent, ["готово"])
    assert config.model == "deepseek-r1"


def test_busy_flag_is_released_when_the_provider_is_missing():
    agent = Agent(config=BeeConfig(provider="nowhere"))
    with pytest.raises(RuntimeError):
        agent.run_sync("hi", session=Session())
    assert agent.is_busy is False


# --- streaming contract ----------------------------------------------------

class _Response:
    def __init__(self, lines):
        self._lines = lines

    def raise_for_status(self):
        return None

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _StreamCtx:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _StreamCtx(self._response)


async def _collect(agen):
    return [item async for item in agen]


def _patch_client(monkeypatch, module, response):
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **k: _Client(response))


def test_openai_compat_streams_kind_text_pairs(monkeypatch):
    from beeagent.providers import openai_compat

    lines = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        "data: [DONE]",
        'data: {"choices":[]}',
        "not-an-event-line",
        'data: {broken json',
    ]
    _patch_client(monkeypatch, openai_compat, _Response(lines))
    provider = openai_compat.OpenAICompatProvider(base_url="https://x/api/v1", api_key="k",
                                                  model="m", name="x", models=("m",))
    assert asyncio.run(_collect(provider.chat_stream([], model="m"))) == [("content", "Hel")]


def test_ollama_streams_kind_text_pairs(monkeypatch):
    from beeagent.providers import ollama

    lines = ['{"message":{"content":"pri"}}', '{"message":{}}', '{"done":true}', ""]
    _patch_client(monkeypatch, ollama, _Response(lines))
    provider = ollama.OllamaProvider(base_url="http://localhost:11434", model="llama3")
    assert asyncio.run(_collect(provider.chat_stream([], model="llama3"))) == [("content", "pri")]


# --- economy cache in the loop ---------------------------------------------

def test_a_cached_tool_call_is_never_replayed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _agent(config=BeeConfig(mode="economy", permissions={"mode": "auto"},
                                   economy={"cache_dir": str(tmp_path / "c")}))
    agent.economy.check_cache = lambda prompt, model: \
        '{"tool": "recorder", "args": {"note": "from cache"}}'

    _run(agent, ['{"tool": "recorder", "args": {"note": "real"}}', "done"])

    assert agent.spy.calls == ["real"], "a cached step must be re-run, not printed"


def test_a_cached_plain_answer_is_served(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = _agent(config=BeeConfig(mode="economy", permissions={"mode": "auto"},
                                   economy={"cache_dir": str(tmp_path / "c")}))
    agent.economy.check_cache = lambda prompt, model: "served from the honey cache"

    answer, events = _run(agent, ["the model is never asked"])

    assert answer == "served from the honey cache"
    assert "economy_hit" in [e for e, _ in events]
    assert agent.spy.calls == []
