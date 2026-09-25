from beeagent.core.context import ContextManager, REMINDER_SUFFIX
from beeagent.i18n import set_lang

import pytest


@pytest.fixture(autouse=True)
def _english(tmp_path, monkeypatch):
    """Language is process-global; every test here states the one it quotes.

    The measured-window cache is redirected too: a measurement made by another
    test (or lying in the working tree) must not silently change what these
    assertions think a model's window is.
    """
    from beeagent.core import windows

    set_lang("en")
    monkeypatch.setattr(windows, "CACHE", tmp_path / "windows.json")


def _msgs(*pairs):
    return [{"role": role, "content": content} for role, content in pairs]


def test_huge_tool_result_is_clipped_not_evicting():
    set_lang("ru")                            # the assertion below quotes the RU marker
    cm = ContextManager(max_tokens=12000)
    built = cm.build_messages(_msgs(
        ("user", "проанализируй C:/bot/userbot.py"),
        ("assistant", '```json\n{"tool": "read", "args": {"path": "userbot.py"}}\n```'),
        ("tool", "[tool result] " + "X" * 400_000),
    ), [])
    text = "\n".join(m["content"] for m in built)
    assert "проанализируй" in text          # the task must never disappear
    assert "обрезано" in text               # the dump came in clipped
    assert cm.dropped == 0                  # nothing had to be evicted...
    # ...but the clip threw most of the dump away, and that is counted. It used
    # to be `trimmed == 0` here: the counter reported nothing while a 400 KB
    # read left the request at 2 KB, so the interface stayed silent about it.
    assert cm.clipped == 1
    assert cm.trimmed == 1
    assert cm.trimmed_tokens > 40_000


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
    if cm.dropped:
        # The summary is what covers an eviction. A clip is not one, so the
        # claim is only checked when something was actually dropped.
        assert any("CONVERSATION SO FAR" in b for b in bodies)


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

    set_lang("ru")                            # the assertion below quotes the RU marker
    agent = Agent(config=BeeConfig())
    seen = []
    script = iter([
        '```json\n{"tool": "read", "args": {"path": "userbot.py"}}\n```',
        "разобралась",
    ])

    async def fake_stream(provider, messages, callback=None, model=""):
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


# --- the small-context amnesia fix ---------------------------------------


def _long_history(turns: int) -> list[dict]:
    history = _msgs(("user", "ЗАДАЧА 1: разбери проект и найди проблемы"))
    for i in range(turns):
        history += [
            {"role": "assistant", "content": f"пункт {i}: " + "подробное обоснование " * 40},
            {"role": "user", "content": f"уточнение {i}: " + "сделай ещё лучше " * 30},
        ]
    history.append({"role": "user", "content": "ЗАДАЧА ФИНАЛЬНАЯ: почини это"})
    return history


def test_dropped_turns_travel_as_a_digest_not_a_hole():
    """What the user asked for: trimming must not send the model a blank page."""
    cm = ContextManager(model="glm-4.7-flash")
    history = _long_history(30)
    built = cm.build_messages(history, [])
    system = built[0]["content"]
    window = "\n".join(m["content"] for m in built[1:])
    assert cm.trimmed > 0, "this history must overflow a small model"
    assert "CONVERSATION SO FAR" in system
    assert any("ЗАДАЧА ФИНАЛЬНАЯ" in m["content"] for m in built)

    # Nothing disappears: every message is either in the window verbatim or
    # named in the digest.
    assert "ЗАДАЧА 1" in system + window, "the opening request must survive"
    assert "пункт 0" in system, "the compressed middle is summarised, not hidden"
    assert "пункт 0" not in window


def test_digest_is_written_in_the_active_language():
    history = _long_history(30)
    set_lang("ru")
    system = ContextManager(model="glm-4.7-flash").build_messages(history, [])[0]["content"]
    assert "ХОД РАЗГОВОРА" in system
    assert "CONVERSATION SO FAR" not in system


def test_request_never_costs_more_than_the_window_allows():
    from beeagent.utils.tokens import count_tokens

    history = _long_history(40)
    for model in ("gpt-4", "glm-4.7-flash", "gpt-4o", "qwen2.5-32b"):
        cm = ContextManager(max_tokens=12000, model=model)
        built = cm.build_messages(history, [])
        used = sum(count_tokens(m["content"], model) for m in built)
        assert used <= cm.max_tokens, f"{model}: {used} tokens sent into a {cm.window} window"


def test_window_follows_the_model_and_an_unknown_one_stays_small():
    from beeagent.core.context import window_for

    assert window_for("llama-3.1-8b-128k") == 32768   # capped, not unlimited
    assert window_for("glm-4-9b-32k") == 32768
    assert window_for("gpt-4o") == 32768              # 128k, capped
    assert window_for("glm-4.7-flash") == 8192        # unrecognised: assume small
    assert ContextManager(model="gpt-4").max_tokens < 8192, "room for the reply is reserved"


def test_max_context_tokens_is_a_ceiling_not_a_pin():
    cm = ContextManager(max_tokens=12000, model="glm-4.7-flash")
    assert cm.window == 8192, "a small model must not be fed a 12000-token request"
    big = ContextManager(max_tokens=12000, model="gpt-4o")
    assert big.window == 12000, "the ceiling still holds a big model down"


def test_cyrillic_is_not_undercounted_for_non_openai_models():
    """The old chars//4 guess read Russian text as half its real size, so an
    oversized request looked like it fit and the endpoint trimmed it."""
    from beeagent.utils.tokens import count_tokens

    text = "проанализируй проект и почини падающие тесты " * 60
    for model in ("glm-4.7-flash", "deepseek-chat", "no-such-model-xyz"):
        assert abs(count_tokens(text, model) - count_tokens(text, "gpt-4")) < 10


def test_the_live_request_is_shrunk_rather_than_dropped():
    cm = ContextManager(model="glm-4.7-flash")
    huge = "требование " * 40000
    built = cm.build_messages([{"role": "user", "content": huge}], [])
    joined = "\n".join(m["content"] for m in built)
    assert "требование" in joined, "even an oversized request must reach the model"
    assert "truncated" in joined


def test_trimmed_reports_a_digest_count_the_ui_can_show():
    cm = ContextManager(model="glm-4.7-flash")
    cm.build_messages(_long_history(60), [])
    assert cm.trimmed > 0


def test_build_messages_never_grows_the_callers_history():
    """The reminder must not be written back into the session's own dicts."""
    history = _flat([("user", "задача"), ("assistant", "ответ")])
    before = [dict(m) for m in history]
    cm = ContextManager(model="gpt-4")
    for _ in range(3):
        built = cm.build_messages(history, [])
        assert any("SYSTEM:" in m["content"] for m in built)     # reminder is sent
    assert history == before                                     # but not stored


def test_the_prompt_still_leaves_room_for_the_conversation():
    """Identity + behaviour + tool catalog must stay a fraction of the window.

    The prompt grows by accretion, and every token of it is a token taken from
    history, so the budget is asserted rather than hoped for.

    Two budgets, because they answer to different people. The strict one covers
    BeeCode's own tools -- what this repository can actually be blamed for. MCP
    tools are counted separately and against the window, not the 3000: a user who
    connects a large MCP server is making their own trade-off, and failing this
    test on their behalf would report a defect that is not in the code.

    The strict number moved to 3400 on 2026-09-24 and both halves of it are
    measured, not guessed: the `<bee-data>` rule that tells the model a tool
    result is data and not an instruction costs 76 tokens (1613 -> 1689), and
    the tool catalog grew from 1227 to 1507 the same day. 3000 was always a
    wish, though, not a guarantee; the guarantee is the second half of this
    test, and it is why the number can be a little wrong without the user's
    question being clipped: whatever the header costs, it is shed before the
    live turn is, at every window down to 1024 tokens.
    """
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.context import SYSTEM_PROMPT
    from beeagent.core.parser import CommandParser
    from beeagent.utils.tokens import count_tokens

    schemas = Agent(config=BeeConfig()).tools.to_schemas()
    own = [s for s in schemas if not s["name"].startswith("mcp_")]
    mcp = [s for s in schemas if s["name"].startswith("mcp_")]

    header = count_tokens(SYSTEM_PROMPT + "\n" + CommandParser().format_tool_prompt(own),
                          "gpt-4")
    assert header < 3400, f"BeeCode's own prompt header costs {header} tokens"

    everything = count_tokens(SYSTEM_PROMPT + "\n"
                              + CommandParser().format_tool_prompt(schemas), "gpt-4")
    assert everything < ContextManager(model="glm-4-9b-32k").max_tokens // 2, (
        f"with {len(mcp)} MCP tools attached the header is {everything} tokens")

    question = ("Почини падение тестов в beeagent/core/context.py и объясни, почему "
                "бюджет истории мог стать отрицательным")
    for cap in (1024, 2048, 4096, 8192):
        manager = ContextManager(model="gpt-4", window=cap)
        built = manager.build_messages([{"role": "user", "content": question}], own)
        sent = sum(count_tokens(str(m["content"]), "gpt-4") for m in built)
        assert sent <= manager.max_tokens, f"{cap}: {sent} tokens into a {cap} window"
        # A 45-token question must arrive entire at any window, and it is the
        # header that pays for it: `shed` says which parts went, so this cannot
        # be quietly bought back by clipping the user.
        assert question in built[-1]["content"], f"{cap}: the question was clipped"
        assert manager.dropped == 0 and manager.clipped == 0, f"{cap}: history was trimmed"


def test_the_pool_models_carry_the_windows_their_owner_states():
    """These twelve were labelled from a generic guess before.

    `qwen` matched `qwen3-coder-480b` and printed 32k against a 256k model, and
    `gemma-3-12b`, `llama-4-maverick` and `glm-5.2` matched no rule at all and got
    the 8k default -- so the picker ranked a 1M model below an 8k one. The values
    are what the provider claims, which is why the interface still marks them "~"
    and `window_for` keeps sending conservatively until /window measure proves it.
    """
    from beeagent.core.context import advertised_window

    stated = {
        "qwen3-coder-480b": 262144, "gemma-3-12b": 131072,
        "llama-4-maverick": 1_000_000, "gpt-5-6-luna": 1_050_000,
        "kimi-k2-6": 262144, "kimi-k2-7-code": 262144,
        "glm-5.2": 1_000_000, "glm-5.3": 1_000_000, "glm-5.3-flash": 1_048_576,
        "grok-4-3": 1_000_000, "grok-4-6": 500_000, "deepseek-v4-flash": 1_048_576,
    }
    for model, tokens in stated.items():
        assert advertised_window(model) == tokens, model
    # the specific id must win over the shorter family rule it sits next to
    assert advertised_window("glm-5.3-flash") > advertised_window("glm-5.3")
