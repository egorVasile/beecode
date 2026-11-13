"""The pinned context strip: green under half, yellow under 80, red past it.

It paints itself above every prompt in both interfaces — no command involved —
so these pin the colors, the math (same ruler as /token) and the one hard rule:
a HUD never eats a turn, whatever the context holds.
"""
import io
from types import SimpleNamespace

from rich.console import Console

from beeagent.ui.components import context_bar


def _ctx(chars, limit=1000, broken=False):
    def build(messages, schemas):
        if broken:
            raise RuntimeError("boom")
        return messages

    return SimpleNamespace(
        config=SimpleNamespace(model="m"),
        agent=SimpleNamespace(
            context=SimpleNamespace(max_tokens=limit, build_messages=build),
            tools=SimpleNamespace(to_schemas=lambda: [])),
        session=SimpleNamespace(
            to_dicts=lambda: [{"role": "user", "content": "x" * chars}]))


def _styles(bar) -> list:
    return [span.style for span in bar.spans]


def test_colors_follow_the_fill(monkeypatch):
    import beeagent.utils.tokens as tokens

    # One char is one token here: the thresholds, not the ruler, are the claim.
    monkeypatch.setattr(tokens, "count_tokens", lambda text, model: len(text))
    assert any("green" in str(style) for style in _styles(context_bar(_ctx(400))))
    assert any("yellow" in str(style) for style in _styles(context_bar(_ctx(600))))
    assert any("red" in str(style) for style in _styles(context_bar(_ctx(900))))


def test_bar_and_percent_render(monkeypatch):
    import beeagent.utils.tokens as tokens

    monkeypatch.setattr(tokens, "count_tokens", lambda text, model: len(text))
    buffer = io.StringIO()
    Console(file=buffer, width=120).print(context_bar(_ctx(400)))
    out = buffer.getvalue()
    assert "\u2588" in out and "40%" in out


def test_broken_context_means_no_strip_not_a_crash():
    assert context_bar(SimpleNamespace()) is None
    assert context_bar(SimpleNamespace(agent=None, session=None)) is None
    assert context_bar(_ctx(10, broken=True)) is None


def test_real_agent_session_renders(tmp_path, monkeypatch):
    """Same objects /token counts — the strip must agree with it, not guess."""
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext

    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    agent = Agent(config=config)
    session = Session()
    session.add_user_message("hello")
    bar = context_bar(ReplContext(agent=agent, config=config, session=session))
    assert bar is not None
    buffer = io.StringIO()
    Console(file=buffer, width=120).print(bar)
    assert "%" in buffer.getvalue()
