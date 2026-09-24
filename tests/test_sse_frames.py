"""SSE frames: the one place a half-read network answer becomes a statement.

Every shape below is fed the way the wire feeds it — an async iterator of lines
with the exact text the endpoint sent: an event split over several `data:` lines,
CRs still attached, `: keep-alive` in the middle, `[DONE]` before the end, and an
answer cut off inside a JSON object. Each test names the text the user would
actually see.
"""
import asyncio
import json

import pytest

from beeagent.i18n import get_lang, set_lang
from beeagent.providers import ollama as ollama_mod
from beeagent.providers import openai_compat as compat_mod
from beeagent.providers.base import ProviderStreamError, sse_payloads
from beeagent.providers.ollama import OllamaProvider
from beeagent.providers.openai_compat import OpenAICompatProvider


@pytest.fixture(autouse=True)
def english():
    """The assertions quote the English wording; the pair is checked separately."""
    previous = get_lang()
    set_lang("en")
    yield
    set_lang(previous)


# --- the shapes of each provider's frames ----------------------------------

def openai_frame(text):
    return 'data: {"choices": [{"delta": {"content": %s}}]}' % json.dumps(text)


def openai_split(text):
    """The same event as the spec allows it: two `data:` lines, one payload.

    An empty `data:` line sits between them, because the endpoints that split a
    frame write one.
    """
    body = '{"choices": [{"delta": '
    tail = '{"content": %s}}]}' % json.dumps(text)
    return ["data: " + body, "data:", "data: " + tail]


def ollama_frame(text):
    return 'data: {"message": {"content": %s}}' % json.dumps(text)


def ollama_split(text):
    return ['data: {"message":', "data:  " + '{"content": %s}}' % json.dumps(text)]


CASES = [
    pytest.param(compat_mod, lambda: OpenAICompatProvider(
        base_url="https://x/api/v1", api_key="k", model="m", name="fake", models=("m",)),
        openai_frame, openai_split, id="openai"),
    pytest.param(ollama_mod, lambda: OllamaProvider(
        base_url="http://localhost:11434", model="llama3"),
        ollama_frame, ollama_split, id="ollama"),
]
DONE = "data: [DONE]"


# --- fakes -----------------------------------------------------------------

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


def feed(monkeypatch, module, lines):
    monkeypatch.setattr(module.httpx, "AsyncClient", lambda *a, **k: _Client(_Response(lines)))


async def _async_lines(lines):
    for line in lines:
        yield line


def seen(pieces):
    """The answer exactly as it reaches the screen."""
    return "".join(text for kind, text in pieces)


def collect(agen):
    return asyncio.run(_collect(agen))


async def _collect(agen):
    return [pair async for pair in agen]


def collect_error(agen):
    """The pieces shown plus the error that followed them, or None."""
    async def drain():
        pieces = []
        try:
            async for pair in agen:
                pieces.append(pair)
        except ProviderStreamError as e:
            return pieces, str(e)
        return pieces, None

    return asyncio.run(drain())


def payloads(lines):
    return asyncio.run(_collect(sse_payloads(_async_lines(lines))))


# --- frame assembly --------------------------------------------------------

def test_several_data_lines_are_one_event_joined_with_a_newline():
    assert payloads(['data: {"choices": [{"delta":', 'data: {"content": "привет"}}]}', ""]) == [
        '{"choices": [{"delta":\n{"content": "привет"}}]}']


def test_an_event_is_closed_by_a_blank_line_and_by_the_end_of_the_stream():
    assert payloads(['data: {"a": 1}', "", 'data: {"b": 2}']) == ['{"a": 1}', '{"b": 2}']


def test_a_comment_is_not_data_and_a_frame_header_is_not_data():
    assert payloads([": keep-alive", ":OPENROUTER PROCESSING", "event: message",
                     "id: 42", "retry: 5000", 'data: {"a": 1}', ""]) == ['{"a": 1}']


def test_a_data_line_of_its_own_carries_the_newline_the_spec_promises():
    """`data: a` + `data: b` is the payload "a\\nb", never "ab"."""
    assert payloads(["data: a", "data: b", ""]) == ["a\nb"]


@pytest.mark.parametrize("marker", ["data: [DONE]", "data:[DONE]", 'data: "[DONE]"', "[DONE]"])
def test_done_ends_the_stream_at_the_frame_level_and_is_never_a_payload(marker):
    assert payloads(['data: {"a": 1}', "", marker, 'data: {"b": 2}', ""]) == ['{"a": 1}']


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_a_two_line_event_is_one_answer_and_not_two_dropped_halves(monkeypatch, module, make,
                                                                   frame, split):
    provider = make()
    feed(monkeypatch, module, split("при") + [""] + split("вет") + ["", DONE])
    assert seen(collect(provider.chat_stream([], model="m"))) == "привет"


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_crlf_line_endings_change_nothing(monkeypatch, module, make, frame, split):
    provider = make()
    lines = []
    for line in split("при") + [""] + [frame("вет")] + ["", DONE]:
        lines.append(line + "\r" if not line.endswith("\r\n") else line)
    lines[-2] = "\r\n"                                  # the closing blank line, CRLF
    feed(monkeypatch, module, lines)
    assert seen(collect(provider.chat_stream([], model="m"))) == "привет"


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_keep_alives_between_pieces_do_not_reach_the_screen(monkeypatch, module, make, frame,
                                                            split):
    provider = make()
    feed(monkeypatch, module, [": keep-alive", frame("а"), "", ": keep-alive",
                               "event: ping", frame("б"), "", ": keep-alive", DONE])
    assert seen(collect(provider.chat_stream([], model="m"))) == "аб"


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_done_mid_stream_stops_it_and_never_becomes_text(monkeypatch, module, make, frame,
                                                         split):
    provider = make()
    feed(monkeypatch, module, [frame("до"), DONE, frame("после"), DONE])
    pieces, error = collect_error(provider.chat_stream([], model="m"))
    assert seen(pieces) == "до"
    assert error is None, "a clean [DONE] is not a failure"


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_junk_after_done_is_never_read(monkeypatch, module, make, frame, split):
    provider = make()
    feed(monkeypatch, module, [frame("а"), DONE, "data: {broken json", "not-an-event-line"])
    assert seen(collect(provider.chat_stream([], model="m"))) == "а"


# --- the honest failures ---------------------------------------------------

@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_a_torn_frame_fails_the_stream_instead_of_looking_like_an_answer(
        monkeypatch, module, make, frame, split):
    """The dangerous shape: the connection died in the middle of an object.

    Skipping the unreadable line used to leave "При" on screen as a complete
    answer; the loop needs the failure so it drops the fragment and asks again.
    """
    provider = make()
    torn = frame("ветstv")[:-4]                         # cut inside the JSON object
    feed(monkeypatch, module, [frame("При"), "", torn])
    pieces, error = collect_error(provider.chat_stream([], model="m"))
    assert seen(pieces) == "При", "the partial text is what got that far"
    assert error and "cannot be read" in error and "not the whole answer" in error


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_a_stream_that_carries_no_text_raises_instead_of_answering_nothing(
        monkeypatch, module, make, frame, split):
    provider = make()
    feed(monkeypatch, module, [": keep-alive", DONE])
    with pytest.raises(ProviderStreamError) as raised:
        collect(provider.chat_stream([], model="m"))
    assert "closed the stream without an answer" in str(raised.value)


def test_both_providers_answer_silence_the_same_way(monkeypatch):
    """One reader in `base`, so no provider gets to invent its own failure."""
    feed(monkeypatch, compat_mod, [DONE])
    first_pieces, first_error = collect_error(
        OpenAICompatProvider(base_url="https://x", name="groqlike").chat_stream([], model="m"))
    feed(monkeypatch, ollama_mod, ['{"done": true}'])
    second_pieces, second_error = collect_error(OllamaProvider().chat_stream([], model="m"))

    assert first_pieces == second_pieces == []
    assert first_error and second_error
    assert "closed the stream without an answer" in first_error
    assert "closed the stream without an answer" in second_error
    assert first_error.startswith("groqlike") and second_error.startswith("ollama")


@pytest.mark.parametrize("module, make, frame, split", CASES)
def test_a_frame_that_names_the_reason_is_told_to_the_user(monkeypatch, module, make, frame,
                                                           split):
    """HTTP 200 + an `error` body is a refusal, not a provider that said nothing."""
    provider = make()
    feed(monkeypatch, module, [frame("а"), '{"error": "no such model: llama99"}',
                               '{"error": {"message": "budget spent"}}'])
    pieces, error = collect_error(provider.chat_stream([], model="m"))
    assert seen(pieces) == "а"
    assert "no such model: llama99" in error


# --- what must still come through untouched --------------------------------

def test_the_answer_may_look_exactly_like_a_frame_or_a_command(monkeypatch):
    """The payload is JSON; the text inside it is whatever the model said."""
    lines, expected = [], ""
    for piece in ["data: [DONE]", "```json", ": not a comment", '{"tool": "x"}', "\r\n"]:
        lines.append(openai_frame(piece))
        lines.append("")
        expected += piece
    feed(monkeypatch, compat_mod, lines + [DONE])
    assert seen(collect(
        OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))) == expected


def test_ollama_still_reads_the_undecorated_ndjson_it_actually_sends(monkeypatch):
    provider = OllamaProvider()
    feed(monkeypatch, ollama_mod, [
        '{"message": {"role": "assistant", "content": "раз"}}',
        '{"message": {"role": "assistant", "content": ""}, "done": false}',
        '{"message": {"role": "assistant", "content": "два"}}',
        '{"message": {"role": "assistant"}, "done": true, "done_reason": "stop"}',
    ])
    assert seen(collect(provider.chat_stream([], model="m"))) == "раздва"


def test_a_model_that_stops_at_an_empty_final_record_is_reported_as_a_failure(monkeypatch):
    """`{"message":{}}` is a closing frame, not an answer of zero characters."""
    provider = OllamaProvider()
    feed(monkeypatch, ollama_mod, ['{"message": {"role": "assistant", "content": ""}}'])
    with pytest.raises(ProviderStreamError):
        collect(provider.chat_stream([], model="m"))


def test_an_error_before_any_text_is_the_error_the_user_sees(monkeypatch):
    feed(monkeypatch, compat_mod, ['{"error": "model id wrong"}', DONE])
    with pytest.raises(ProviderStreamError) as raised:
        collect(OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))
    assert "model id wrong" in str(raised.value)


# --- the contract the rest of BeeCode leans on -----------------------------

def test_the_failure_is_a_runtime_error_so_the_loop_treats_it_as_a_provider_fault():
    """`core.agent` retries on `Exception`; a silent stream must join them."""
    assert issubclass(ProviderStreamError, RuntimeError)


@pytest.mark.parametrize("marker", ["data: [DONE]", "data:[DONE]", 'data: "[DONE]"', "[DONE]"])
def test_every_spelling_of_done_ends_the_stream_without_one_letter_on_screen(monkeypatch,
                                                                            marker):
    feed(monkeypatch, compat_mod, [openai_frame("а"), marker, openai_frame("б")])
    pieces, error = collect_error(
        OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))
    assert seen(pieces) == "а"
    assert "[DONE]" not in seen(pieces)
    assert error is None, "a clean [DONE] is not a failure"


def test_the_last_event_needs_no_blank_line_after_it(monkeypatch):
    """An endpoint that hangs up right after the answer still gives one."""
    feed(monkeypatch, compat_mod, openai_split("готово"))
    pieces, error = collect_error(
        OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))
    assert seen(pieces) == "готово"
    assert error is None


def test_both_honest_failures_have_a_russian_wording_too(monkeypatch):
    """Every string here is a pair; an untranslated one is a bug in its own right."""
    previous = get_lang()
    set_lang("ru")
    try:
        feed(monkeypatch, compat_mod, [DONE])
        silent = collect_error(
            OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))
        feed(monkeypatch, compat_mod, [openai_frame("При"), 'data: {"choices": [{"delta": {"x"'])
        torn = collect_error(
            OpenAICompatProvider(base_url="https://x", name="fake").chat_stream([], model="m"))
    finally:
        set_lang(previous)
    assert "закрыл поток, не отправив ответ" in silent[1]
    assert "не читается" in torn[1] and "не весь ответ" in torn[1]
