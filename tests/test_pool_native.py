"""The pool's native tool protocol and its front-door patience.

These promises hold without the server file: the pool ships OpenAI's own
shape, so a turn can ask natively — the schemas in `tools`, the calls back
in `message.tool_calls`. And the gate a sleeping platform serves (an HTML
wake page instead of the pool's JSON) is waited out with a fresh nonce per
attempt, because the same signed bytes sent twice answer "a replay".
"""
import asyncio
import json

import pytest

from beeagent.i18n import get_lang, set_lang
from beeagent.core.parser import CommandParser
from beeagent.providers import pool as pool_mod
from beeagent.providers.pool import PoolError, PoolProvider


@pytest.fixture(autouse=True)
def install_key_in_tmp(tmp_path, monkeypatch):
    monkeypatch.setenv("BEECODE_POOL_KEY_FILE", str(tmp_path / "pool-key.json"))


@pytest.fixture(autouse=True)
def english():
    previous = get_lang()
    set_lang("en")
    yield
    set_lang(previous)


class FakeResponse:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = text if text is not None else json.dumps(self._body)
        self.headers = {"content-type": "application/json"
                        if text is None else "text/html"}

    def json(self):
        return self._body


class Page:
    """One scripted answer to a POST: (status, json-body [, raw html])."""

    def __init__(self, attempts):
        self.attempts = attempts
        self.seen: list[dict] = []

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, content=None, headers=None):
        index = len(self.seen)
        nonce = headers.get("X-Seat-Nonce", "")
        self.seen.append({"url": url, "nonce": nonce,
                          "body": json.loads(content or b"{}")})
        if index < len(self.attempts):
            page = self.attempts[index]
            return FakeResponse(status=page[0], body=page[1],
                                text=page[2] if len(page) > 2 else None)
        raise AssertionError("the pool ran out of script")


@pytest.fixture()
def no_wait(monkeypatch):
    monkeypatch.setattr(pool_mod, "COLD_START_WAIT", 0)
    monkeypatch.setattr(PoolProvider, "GATE_WAIT", 0.0)


WIRE = "http://pool.invalid"
SEAT = "seat-token"
SCHEMAS = [{"name": "write", "description": "Create a file. Expects path and content.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"}}, "required": ["path", "content"]}}]
MESSAGE_HISTORY = [
    {"role": "user", "content": "hi"},
    {"role": "assistant", "content": "",
     "tool_calls": [{"tool": "write", "args": {"path": "a.txt", "content": "x"}}]},
    {"role": "tool", "content": "done"},
]


def test_the_pool_owns_the_native_protocol():
    assert PoolProvider.supports_tools is True


def test_complete_sends_schemas_and_reads_calls_back(no_wait, monkeypatch):
    """The model named the tool natively: OpenAI's shape out, BeeCode's in."""
    wire = Page([
        (200, {"choices": [{"message": {
            "content": "",
            "tool_calls": [{"id": "call_0001", "type": "function",
                            "function": {"name": "write",
                                         "arguments": '{"path": "hi.txt", "content": "ok"}'}}]}}]}, None),
    ])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    answer = asyncio.run(provider.complete(MESSAGE_HISTORY, "m", SCHEMAS))

    assert answer == {"text": "", "tool_calls":
                      [{"tool": "write", "args": {"path": "hi.txt", "content": "ok"}}]}
    sent = wire.seen[0]["body"]
    assert sent["stream"] is False
    assert sent["tool_choice"] == "auto"
    assert [tool["function"]["name"] for tool in sent["tools"]] == ["write"]
    assert sent["tools"][0]["function"]["description"] == "Create a file."
    assert sent["messages"][-1] == {
        "role": "tool", "tool_call_id": "call_0001", "content": "done"}
    assert sent["messages"][-2]["tool_calls"][0]["function"]["name"] == "write"


def test_complete_gets_a_nonstandard_message_and_lives(no_wait, monkeypatch):
    """A list in `message` is an empty answer, not a TypeError."""
    wire = Page([(200, {"choices": [{"message": []}]}, None)])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)
    assert asyncio.run(provider.complete(MESSAGE_HISTORY, "m")) == \
        {"text": "", "tool_calls": []}


def test_broken_argument_json_reads_as_no_arguments(no_wait, monkeypatch):
    """Half-written arguments: the call survives with nothing in its hands."""
    wire = Page([(200, {"choices": [{"message": {
        "content": "",
        "tool_calls": [{"function": {"name": "write", "arguments": '{"path": '}}]}}]}, None)])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)
    answer = asyncio.run(provider.complete(MESSAGE_HISTORY, "m"))
    assert answer["tool_calls"] == [{"tool": "write", "args": {}}]


def test_a_wake_page_is_waited_out_with_a_fresh_nonce(no_wait, monkeypatch):
    """502 + HTML is the platform's front door, not the pool: ask again."""
    gate_page = ("<!doctype html><html lang=\"ru\"><head><title>Постой"
                 "</title></head><body>сервис просыпается</body></html>")
    wire = Page([(502, {}, gate_page), (502, {}, gate_page),
                 (200, {"choices": [{"message": {"content": "жужж"}}]}, None)])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    answer = asyncio.run(provider._chat(
        [{"role": "user", "content": "привет"}], "m", SEAT))

    assert answer == "жужж"
    nonces = {row["nonce"] for row in wire.seen}
    assert len(nonces) == len(wire.seen) == 3, "every ask carries fresh signed bytes"


def test_a_gate_that_never_opens_names_the_sleep(no_wait, monkeypatch):
    gate_page = "<html><body>заснуло</body></html>"
    wire = Page([(502, {}, gate_page)]
                * (pool_mod.PoolProvider.GATE_RETRIES + 1))
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    with pytest.raises(PoolError) as raised:
        asyncio.run(provider._chat([{"role": "user", "content": "п"}], "m", SEAT))

    assert "wake up" in str(raised.value) or "пробуждени" in str(raised.value)
    assert len(wire.seen) == pool_mod.PoolProvider.GATE_RETRIES + 1


def test_the_replay_guard_is_answered_with_a_fresh_signature(no_wait, monkeypatch):
    """The front end re-sent the same POST; re-sign, do not beg."""
    wire = Page([(401, {"error": "this exact request was already used — a replay"},
                  None),
                 (200, {"choices": [{"message": {"content": "жужж"}}]}, None)])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    answer = asyncio.run(provider._chat(
        [{"role": "user", "content": "привет"}], "m", SEAT))

    assert answer == "жужж"
    assert len(wire.seen) == 2
    assert wire.seen[0]["nonce"] != wire.seen[1]["nonce"]


def test_a_second_replay_is_not_mining_the_pool(no_wait, monkeypatch):
    wire = Page([(401, {"error": "already used — a replay"}, None)] * 4)
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    with pytest.raises(PoolError) as raised:
        asyncio.run(provider._chat([{"role": "user", "content": "п"}], "m", SEAT))

    assert "enroll" in str(raised.value) or "место" in str(raised.value)
    assert len(wire.seen) == 2, "one retry, then the honest error"


def test_a_429_still_uses_the_server_s_cadence(no_wait, monkeypatch):
    wire = Page([(429, {"error": "rate limit"}, None),
                 (200, {"choices": [{"message": {"content": "жужж"}}]}, None)])
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)
    monkeypatch.setattr(pool_mod.httpx, "AsyncClient", wire)

    answer = asyncio.run(provider._chat(
        [{"role": "user", "content": "привет"}], "m", SEAT))

    assert answer == "жужж"


def test_a_streamed_tool_call_is_reread_as_the_shed_block(monkeypatch):
    """Tool deltas ride the stream after all: gathered once, re-emitted as the
    block the shed parser reads — nothing that called is lost to silence."""
    provider = pool_mod.PoolProvider(url=WIRE, token=SEAT)

    async def raw_stream(messages, model, token):
        carry: list[dict] = []
        for event in (
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function":
                {"name": "write", "arguments": '{"path": "hi.txt"'}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function":
                {"arguments": ', "content": "ok"}'}}]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ):
            for piece in pool_mod._pieces(event, "the pool", carry):
                yield piece

    async def collect():
        return [pair async for pair in provider.chat_stream(
            [{"role": "user", "content": "hi"}], "m")]

    monkeypatch.setattr(provider, "_stream", raw_stream)
    answers = asyncio.run(collect())
    block = "".join(text for _, text in answers)
    parsed = CommandParser().parse(block)
    assert parsed.has_commands
    assert parsed.commands[0].tool == "write"
    assert parsed.commands[0].args == {"path": "hi.txt", "content": "ok"}
