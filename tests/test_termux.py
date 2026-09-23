"""BeeCode on a phone.

Termux has no C compiler and no Rust, and PyPI publishes no wheel for `g4f` or
`tiktoken` there, so `pip install beecode` on Android leaves both out. That is a
deal, not a loss: the keyless answers and the exact token count go away, and
everything the agent actually *does* — read, write, edit, grep, run a command,
talk to a model through a pool or a key — has to keep working.

These tests run the real loop with those two modules missing, because that is
what a phone looks like to the import machinery.
"""
import asyncio
import sys

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.utils.tokens import count_tokens


@pytest.fixture()
def phone(monkeypatch, tmp_path):
    """An environment where the extension-built packages are not installed.

    `None` in sys.modules makes `import g4f` raise ImportError, which is the
    same answer pip gives when it never installed the package.
    """
    for name in ("g4f", "g4f.Provider", "tiktoken"):
        monkeypatch.setitem(sys.modules, name, None)
    monkeypatch.chdir(tmp_path)
    return tmp_path


class Scripted:
    """A native endpoint that replays a prepared conversation."""

    name = "scripted"
    supports_tools = True

    def __init__(self, steps):
        self.steps = list(steps)
        self.seen = []

    async def complete(self, messages, model="", tools=None):
        self.seen.append({"messages": messages, "tools": tools})
        return dict(self.steps.pop(0))


def _agent(phone, steps, provider="scripted"):
    # provider names the scripted endpoint: with g4f missing, the agent would
    # otherwise hand the turn to the pool, which is a different test.
    config = BeeConfig(model="m", provider=provider)
    agent = Agent(config=config, workdir=str(phone))
    endpoint = Scripted(steps)
    agent.providers.register(endpoint)
    agent.providers.select = lambda name: endpoint
    agent.permissions.mode = "auto"
    return agent, endpoint


def test_the_agent_starts_without_the_packages_android_cannot_build(phone):
    agent = Agent(config=BeeConfig(), workdir=str(phone))

    assert agent.tools.list_names(), "the tools are the product"
    for name in ("read", "write", "edit", "bash", "grep", "glob", "list_directory", "git"):
        assert agent.tools.get(name) is not None, name
    assert "g4f" in agent.providers.list_names(), \
        "the slot stays advertised; it says what is wrong when it is used"


def test_a_turn_that_writes_a_file_and_reports_it_needs_neither_g4f_nor_tiktoken(phone):
    agent, endpoint = _agent(phone, [
        {"text": "", "tool_calls": [{"tool": "write",
                                     "args": {"path": "hello.py",
                                              "content": "print('пчела')\n"}}]},
        {"text": "файл записан", "tool_calls": []},
    ])

    answer = asyncio.run(agent.run("создай hello.py", session=Session()))

    assert answer == "файл записан"
    assert (phone / "hello.py").read_text(encoding="utf-8") == "print('пчела')\n"
    assert endpoint.steps == [], "both turns came from the scripted endpoint"


def test_the_shell_tool_runs_a_real_command(phone):
    agent, _ = _agent(phone, [
        {"text": "", "tool_calls": [{"tool": "bash", "args": {"command": "echo пчела"}}]},
        {"text": "готово", "tool_calls": []},
    ])
    events = []

    answer = asyncio.run(agent.run("скажи пчела", session=Session(),
                                   callback=lambda e, d: events.append((e, d))))

    assert answer == "готово"
    ended = [d for e, d in events if e == "tool_end"]
    assert ended and not ended[0]["error"], ended
    assert "пчела" in str(ended[0]["output"])


def test_token_budgeting_survives_without_tiktoken(phone):
    """Counting goes approximate, which is the safe direction: an over-count
    trims the history early instead of sending a request the model will drop."""
    assert count_tokens("привет, пчела") > 0
    assert count_tokens("") == 0


def test_a_phone_without_g4f_is_answered_by_the_pool(tmp_path, monkeypatch):
    """The default provider is a compiled package Android cannot install. Left
    alone, a phone's first question would read as a broken agent; the pool is the
    other half of the same promise, so the turn goes there — and says so."""
    from beeagent.providers.pool import PoolProvider

    agent = Agent(config=BeeConfig(provider="g4f"), workdir=str(tmp_path))
    pool = agent.providers.get("pool")
    pool.url, pool.token = "https://pool.invalid", "a-seat"
    monkeypatch.setattr(Agent, "_g4f_installed", staticmethod(lambda: False))
    events = []

    provider = agent._provider_or_pool(lambda e, d: events.append((e, d)))

    assert provider.name == "pool"
    assert agent.config.provider == "pool", "the switch is real, not cosmetic"
    assert ("provider_fallback", {"from": "g4f", "to": "pool", "seat": True}) in events


def test_a_desktop_with_g4f_installed_is_never_moved_off_it(monkeypatch):
    """The fallback is for the machine that cannot have g4f, not for a provider
    the user chose away from."""
    agent = Agent(config=BeeConfig(provider="g4f"), workdir=".")
    monkeypatch.setattr(Agent, "_g4f_installed", staticmethod(lambda: True))
    events = []

    provider = agent._provider_or_pool(lambda e, d: events.append(e))

    assert provider.name == "g4f"
    assert events == []
