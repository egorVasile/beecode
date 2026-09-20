"""Context-window discovery: parsing provider refusals and climbing the ladder."""
import asyncio
import json

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core import windows
from beeagent.core.context import ContextManager, window_for
from beeagent.core.session import Session
from beeagent.ui.commands import ReplContext, dispatch


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(windows, "CACHE", tmp_path / ".beeagent" / "windows.json")
    return tmp_path


# --- reading a limit out of an error ---------------------------------------

@pytest.mark.parametrize("message, expected", [
    ("This model's maximum context length is 8192 tokens. However, you requested 10000.", 8192),
    ("prompt is too long: 200001 tokens > 128000 maximum", 128000),
    ("Context window of 32768 tokens exceeded", 32768),
    ("max_tokens exceeded: limit 4096", 4096),
    ("too many tokens to process: 90000", 90000),
])
def test_limit_is_parsed(message, expected):
    assert windows.limit_from_error(message) == expected


@pytest.mark.parametrize("message", [
    "context length exceeded",              # no number at all
    "Invalid argument",
    "",
])
def test_no_invention_when_the_error_says_nothing(message):
    assert windows.limit_from_error(message) is None


# --- cache ------------------------------------------------------------------

def test_measured_value_is_remembered_and_wins():
    assert window_for("gpt-4o") == 32768          # capped guess, not the real 128k
    windows.remember("gpt-4o", 128000)
    assert windows.measured("gpt-4o") == 128000
    assert window_for("gpt-4o") == 128000         # measurement beats a guess
    assert ContextManager(model="gpt-4o").max_tokens > 12000


def test_a_broken_cache_is_ignored_not_fatal():
    windows.CACHE.parent.mkdir(parents=True, exist_ok=True)
    windows.CACHE.write_text("{not json", encoding="utf-8")
    assert windows.measured("gpt-4o") is None


# --- the ladder -------------------------------------------------------------

class Refusing:
    """Accepts prompts up to `limit` tokens, then errors like a real endpoint."""

    def __init__(self, limit, named=True):
        self.limit = limit
        self.named = named
        self.sizes = []

    async def chat(self, messages, model=""):
        from beeagent.utils.tokens import count_tokens

        size = count_tokens(str(messages[0]["content"]), "gpt-4")
        self.sizes.append(size)
        if size > self.limit:
            raise RuntimeError(
                f"maximum context length is {self.limit} tokens, got {size}"
                if self.named else "context length exceeded")
        return "ok"


def test_probe_uses_the_number_the_endpoint_shouts_back():
    endpoint = Refusing(5000)
    found = asyncio.run(windows.probe("glm-test", endpoint, ceiling=16384))
    assert found == 5000
    assert windows.measured("glm-test") == 5000
    assert len(endpoint.sizes) == 3 and endpoint.sizes[0] >= 2048   # climbed, then refused


def test_probe_falls_back_to_the_last_size_that_fitted():
    endpoint = Refusing(5000, named=False)
    found = asyncio.run(windows.probe("glm-silent", endpoint, ceiling=16384))
    assert found == 4096, "an unhelpful error still narrows the window"


def test_probe_reports_nothing_when_the_endpoint_never_refuses():
    endpoint = Refusing(10 ** 9)
    assert asyncio.run(windows.probe("endless", endpoint, ceiling=4096)) == 4096


# --- the command ------------------------------------------------------------

def test_window_command_reports_the_source_of_the_number():
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    out = dispatch(ctx, "/window gpt-4o").output.plain
    assert "context window" in out and "guessed" in out

    windows.remember("gpt-4o", 128000)
    assert "measured" in dispatch(ctx, "/window gpt-4o").output.plain


def test_window_measure_without_an_agent_says_so():
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    assert "agent" in dispatch(ctx, "/window measure gpt-4o").output.plain.lower()
