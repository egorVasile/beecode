import asyncio

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ui.commands import dispatch
from beeagent.ui.components import ResponseStream, LAST_THINKING


def _stream():
    s = ResponseStream()
    s.on_status()
    return s


def test_status_prints_pending_state(capsys):
    _stream()
    out = capsys.readouterr().out
    assert "💬" in out
    assert "готовит пельмешки" in out or "..." in out


def test_thinking_shows_indicator(capsys):
    s = _stream()
    s.on_thinking("думаю над задачей")
    out = capsys.readouterr().out
    assert "thinking" in out


def test_answer_streams_in_whole_lines(capsys):
    """Nothing may reach the screen ending mid-line.

    prompt_toolkit repaints the prompt after every write, and a write that
    stops in the middle of a line gets its beginning overwritten — the answer
    appeared to lose letters. Lines are therefore released on their newline.
    """
    s = _stream()
    s.on_content("Привет! Чем ")
    s.on_content("помочь?")
    assert "Привет!" not in capsys.readouterr().out, "a partial line stays buffered"
    s.on_content("\nДальше.")
    out = capsys.readouterr().out
    assert "Привет! Чем помочь?" in out


def test_an_answer_without_a_newline_is_not_lost(capsys):
    s = _stream()
    s.on_content("короткий ответ без переноса")
    s.on_done()
    assert "короткий ответ без переноса" in capsys.readouterr().out


def test_a_long_line_still_shows_while_streaming(capsys):
    s = _stream()
    s.on_content("x" * (ResponseStream.LINE_FLUSH_CHARS + 60))
    # rich wraps the block into terminal lines, so count letters, not a substring
    assert capsys.readouterr().out.count("x") >= ResponseStream.LINE_FLUSH_CHARS


def test_tool_json_payload_is_hidden(capsys):
    s = _stream()
    s.on_content('{"tool": "read", "args": {"path": "x.py"}}')
    s.on_tool_start()
    s.on_done()
    out = capsys.readouterr().out
    assert '"tool": "read"' not in out


def test_json_looking_final_answer_is_printed(capsys):
    s = _stream()
    s.on_content('```json\n{"note": "not a tool"}\n```')
    s.on_done()
    out = capsys.readouterr().out
    assert '"note"' in out


def test_peek_opens_last_10_lines(capsys):
    s = _stream()
    s.on_thinking("\n".join(f"строка {i}" for i in range(1, 16)))
    s.peek()
    out = capsys.readouterr().out
    for i in range(6, 16):   # last 10 lines
        assert f"строка {i}" in out
    assert "строка 5" not in out


def test_peek_keeps_streaming_live(capsys):
    s = _stream()
    s.on_thinking("первая\n")
    s.peek()
    capsys.readouterr()
    s.on_thinking("вторая\nтретья")
    out = capsys.readouterr().out
    assert "вторая" in out


def test_done_stores_last_thinking(capsys):
    import beeagent.ui.components as comp
    old = comp.LAST_THINKING
    try:
        s = _stream()
        s.on_thinking("секретные мысли")
        s.on_done()
        out = capsys.readouterr().out
        assert "reasoning" in out
        assert comp.LAST_THINKING == "секретные мысли"
    finally:
        comp.LAST_THINKING = old


def test_empty_response_warning(capsys):
    s = _stream()
    s.on_done()
    out = capsys.readouterr().out
    assert "empty answer" in out


def test_cache_hit_response_renders(capsys):
    s = ResponseStream()
    s.on_response("готовый ответ из кэша")
    out = capsys.readouterr().out
    assert "готовый ответ из кэша" in out


def test_thinking_command_dispatches_pager_action():
    ctx = type("Ctx", (), {})()
    res = dispatch(ctx, "/thinking")
    assert res.action == "thinking_pager"


def test_agent_delivers_pending_queue_with_request():
    class FakeProvider:
        async def chat(self, messages, model=None, stream=False):
            return "ок"

    from beeagent.core.agent import Agent

    agent = Agent(config=BeeConfig())
    agent.providers.select = lambda name: FakeProvider()
    agent.pending.put("второй вопрос")

    events = []
    session = Session()
    res = asyncio.run(agent.run(
        "первый вопрос",
        session=session,
        callback=lambda e, d: events.append((e, d)),
    ))

    assert res == "ок"
    assert any(e == "queued_sent" for e, _ in events)
    assert any(e == "status" for e, _ in events)
    contents = [m.content for m in session.messages]
    assert "первый вопрос" in contents
    assert "второй вопрос" in contents


def test_fallback_chat_answer_is_emitted_as_delta():
    """A non-stream provider answer must reach the UI (empty-response bug)."""
    from beeagent.core.agent import Agent

    class ChatOnly:
        async def chat(self, messages, model=None, stream=False):
            return "Привет! Чем помочь?"

    agent = Agent(config=BeeConfig())
    agent.providers.select = lambda name: ChatOnly()

    events = []
    res = asyncio.run(agent.run(
        "ку",
        session=Session(),
        callback=lambda e, d: events.append((e, d)),
    ))

    assert res == "Привет! Чем помочь?"
    deltas = [d.get("text", "") for e, d in events if e == "stream_delta"]
    assert "Привет! Чем помочь?" in "".join(deltas)
    assert any(e == "done" for e, _ in events)


def test_broken_stream_then_fallback_resets_and_prints_once():
    """A stream that dies mid-answer must not duplicate text on fallback."""
    from beeagent.core.agent import Agent
    from beeagent.ui.components import ResponseStream

    class FlakyStream:
        async def chat_stream(self, messages, model=None):
            yield ("content", "При")
            raise RuntimeError("connection cut")

        async def chat(self, messages, model=None, stream=False):
            return "Привет!"

    agent = Agent(config=BeeConfig())
    agent.providers.select = lambda name: FlakyStream()

    stream = ResponseStream()

    def cb(event, data):
        if event == "stream_delta":
            stream.on_content(data["text"])
        elif event == "stream_reset":
            stream.on_reset()

    events = []
    res = asyncio.run(agent.run(
        "ку",
        session=Session(),
        callback=lambda e, d: (events.append((e, d)), cb(e, d)) and None,
    ))

    assert res == "Привет!"
    names = [e for e, _ in events]
    assert "stream_reset" in names
    # after the reset the UI buffer holds only the fallback answer
    assert stream._text == "Привет!"


def test_on_reset_keeps_thinking_clears_answer():
    from beeagent.ui.components import ResponseStream

    s = ResponseStream()
    s.on_status()
    s.on_thinking("мысли")
    s.on_content("фрагмент")
    s.on_reset()
    assert s._text == ""
    assert s._thinking == "мысли"


# --- raw tool payloads must never reach the screen --------------------------

TOOL_FENCE = 'Сейчас посмотрю.\n```json\n{"tool": "bash", "args": {"command": "ls"}}\n```'
SAMPLE_FENCE = "пример:\n```python\nprint('привет')\n```"


def _chunks(text, size=3):
    """Stream a payload the way a real provider does: in ragged deltas."""
    return [text[i:i + size] for i in range(0, len(text), size)]


def test_fenced_tool_call_after_prose_is_hidden(capsys):
    s = _stream()
    for chunk in _chunks(TOOL_FENCE):
        s.on_content(chunk)
    s.on_tool_start()
    s.on_done()
    out = capsys.readouterr().out
    assert "Сейчас посмотрю." in out
    assert '"tool"' not in out and "```" not in out


def test_fenced_tool_call_never_flashes_mid_stream(capsys):
    # The old code printed everything that did not start the turn, so the raw
    # JSON flashed before the tool ran. It must not appear at any point.
    s = _stream()
    text = 'гляну\n```json\n{"tool": "read", "args": {"path": "a.py"}}\n```\nготово'
    printed = ""
    for chunk in _chunks(text, 7):
        s.on_content(chunk)
        printed += capsys.readouterr().out      # reading drains the capture
        assert '"tool": "read"' not in printed
    s.on_tool_start()
    s.on_done()
    printed += capsys.readouterr().out
    assert "гляну" in printed and "готово" in printed


def test_code_sample_fence_is_still_printed(capsys):
    s = _stream()
    for chunk in _chunks(SAMPLE_FENCE):
        s.on_content(chunk)
    s.on_done()
    out = capsys.readouterr().out
    assert "```python" in out and "print('привет')" in out


def test_inline_backticks_pass_through(capsys):
    s = _stream()
    for chunk in _chunks("выполни `ls -la`, это список"):
        s.on_content(chunk)
    s.on_done()
    out = capsys.readouterr().out.replace("\n", "")
    assert "выполни `ls -la`, это список" in out


def test_unclosed_fence_is_printed_at_done(capsys):
    s = _stream()
    s.on_content('```json\n{"note": "обрывок"}')
    s.on_done()
    assert "обрывок" in capsys.readouterr().out


def test_a_huge_unclosed_tool_fence_never_hits_the_screen(capsys):
    """The screenshot case: a write call carrying a whole HTML page.

    The model stopped after the object without closing the fence, the payload
    grew past FENCE_MAX, and the renderer dumped raw JSON across the terminal
    instead of letting the parser run it.
    """
    s = _stream()
    body = ('{"tool": "write", "args": {"path": "site/index.html", "content": "'
            + '<html>\\n' * 2000 + '"}}')
    s.on_content("```json\n" + body)
    s.on_done()
    out = capsys.readouterr().out
    assert '"tool": "write"' not in out, "a payload that will run is not an answer"
    assert "index.html" not in out


def test_unknown_tool_is_a_note_not_a_red_error(capsys):
    from beeagent.ui.repl import handle_callback
    handle_callback("tool_unknown", {"tool": "read_directory"})
    out = capsys.readouterr().out
    assert "read_directory" in out
    assert "Error" not in out and "failed" not in out


def test_typo_tool_is_rewritten_visibly(capsys):
    from beeagent.ui.repl import handle_callback
    handle_callback("tool_renamed", {"from": "writw", "to": "write"})
    out = capsys.readouterr().out
    assert "writw" in out and "write" in out
    assert "Error" not in out
