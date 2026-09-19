from beeagent.core.context import ContextManager, REMINDER_SUFFIX


def _msgs(*pairs):
    return [{"role": role, "content": content} for role, content in pairs]


def test_huge_tool_result_is_clipped_not_evicting():
    cm = ContextManager(max_tokens=12000)
    built = cm.build_messages(_msgs(
        ("user", "проанализируй C:/bot/userbot.py"),
        ("assistant", '```json\n{"tool": "read", "args": {"path": "userbot.py"}}\n```'),
        ("tool", "[tool result] " + "X" * 400_000),
    ), [])
    text = "\n".join(m["content"] for m in built)
    assert "проанализируй" in text          # the task must never disappear
    assert "обрезано" in text               # the dump came in clipped
    assert cm.trimmed == 0                  # nothing had to be dropped


def test_one_oversized_message_does_not_erase_the_conversation():
    # The old window builder stopped at the first message that did not fit, so
    # a single big tool dump left the model with no history at all and it
    # answered "I'm ready to help — what would you like me to do?".
    history = _msgs(("user", "разбери проект"))
    history.append(("tool", "огромный вывод " + "Z" * 300_000))
    history += [("assistant", f"шаг {i}: " + "y" * 300) for i in range(12)]
    cm = ContextManager(max_tokens=12000)
    built = cm.build_messages(_flat(history), [])
    bodies = [m["content"] for m in built]
    assert any("разбери проект" in b for b in bodies)
    assert sum("шаг" in b for b in bodies) >= 5
    assert any("не влезли в контекст" in b for b in bodies) == (cm.trimmed > 0)


def _flat(pairs):
    """_msgs() output plus raw tuples, so tests can mix both forms."""
    return [p if isinstance(p, dict) else {"role": p[0], "content": p[1]} for p in pairs]


def test_trimmed_count_is_reported_for_the_ui():
    cm = ContextManager(max_tokens=4000)
    history = _flat([("user", "задача")] + [("assistant", f"шаг {i} " + "y" * 3000) for i in range(12)])
    cm.build_messages(history, [])
    assert cm.trimmed > 0


def test_tool_contract_survives_an_empty_window():
    cm = ContextManager(max_tokens=2000)
    built = cm.build_messages(_msgs(("assistant", "a" * 60_000)), [])
    assert built[0]["role"] == "system"
    assert REMINDER_SUFFIX in built[0]["content"]


def test_task_survives_a_giant_tool_result(monkeypatch):
    """End-to-end guard for the amnesia bug: one huge read must not wipe context."""
    import asyncio
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session

    agent = Agent(config=BeeConfig())
    seen = []
    script = iter([
        '```json\n{"tool": "read", "args": {"path": "userbot.py"}}\n```',
        "разобралась",
    ])

    async def fake_stream(provider, messages, callback=None):
        seen.append([dict(m) for m in messages])
        text = next(script)
        if callback:
            callback("stream_delta", {"text": text})
        return text

    class _Result:
        output = "Z" * 300_000
        error = False
        metadata: dict = {}

    monkeypatch.setattr(agent, "_stream_response", fake_stream)
    monkeypatch.setattr(agent.tools.get("read"), "execute", lambda path: _Result())

    asyncio.run(agent.run("проанализируй userbot.py", session=Session()))

    assert len(seen) == 2, "the tool round should continue the same session"
    second_call = "\n".join(m["content"] for m in seen[1])
    assert "проанализируй userbot.py" in second_call
    assert "обрезано" in second_call
