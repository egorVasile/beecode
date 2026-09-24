from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable
import json

DONE_SENTINEL = "[DONE]"
_DONE_ALIASES = (DONE_SENTINEL, '"[DONE]"')


class ProviderStreamError(RuntimeError):
    """A stream that cannot honestly be reported as an answer.

    Two shapes, and they are the same failure from the user's side: the endpoint
    closed without sending any text, and a frame arrived torn so the text on
    screen is only part of the answer.
    """


def _who(source: str) -> str:
    """The endpoint, named — in the language the user is reading in."""
    from beeagent.i18n import L
    return source or L("the endpoint", "эндпоинт")


def _stream_error(english: str, russian: str) -> ProviderStreamError:
    from beeagent.i18n import L
    return ProviderStreamError(L(english, russian))


async def sse_payloads(lines: AsyncIterator[str]) -> AsyncIterator[str]:
    """Reassemble SSE events out of an async line source: one payload per event.

    `aiter_lines()` hands over lines, not events, and reading one line as if it
    were one event is exactly where a half-answer begins:

    * several `data:` lines belong to ONE event and join with "\\n" (the spec);
    * an empty line closes the event — and so does the end of the stream, which
      is how the last event arrives on every endpoint we talk to;
    * `: keep-alive` is a comment and `event:`/`id:`/`retry:` name the envelope;
      none of them carries answer text;
    * a bare JSON line is an event by itself — Ollama answers in newline-delimited
      JSON rather than SSE, and a proxy that forgets the `data:` prefix does too;
    * `[DONE]`, quoted or not, ends the stream and is never a payload.
    """
    pending: list[str] = []

    def closed() -> list[str]:
        """The buffered event, if any, as the payload list to hand out."""
        if not pending:
            return []
        payload = "\n".join(pending)
        pending.clear()
        return [payload]

    async for raw in lines:
        line = (raw if isinstance(raw, str) else "").rstrip("\r\n")
        text = line.strip()
        if not text:
            for payload in closed():
                yield payload
            continue
        if line.startswith(":"):
            continue                        # comment / keep-alive, not data
        if text in _DONE_ALIASES:
            for payload in closed():
                yield payload
            return
        field, sep, value = line.partition(":")
        if sep and field in ("event", "id", "retry"):
            continue
        if sep and field == "data":
            value = value[1:] if value.startswith(" ") else value
            if not value.strip():
                continue
            if value.strip() in _DONE_ALIASES:
                for payload in closed():
                    yield payload
                return
            pending.append(value)           # keep the spacing: the answer is in it
            continue
        if text.startswith(("{", "[")):
            for payload in closed():
                yield payload
            yield line                      # one JSON record per line
    for payload in closed():
        yield payload                       # an event the stream ended on, no blank line


def check_error_frame(event: dict, source: str = "") -> None:
    """A frame that reports a failure is not an empty answer.

    Ollama and several gateways answer 200 and then put the reason in the stream
    body. Dropping it turns "no such model" into "nothing came through", which
    sends the user to change the provider instead of the model name.
    """
    error = event.get("error")
    reason = ""
    if isinstance(error, str):
        reason = error.strip()
    elif isinstance(error, dict):
        reason = str(error.get("message") or error.get("type") or "").strip()
    if reason:
        raise _stream_error(f"{_who(source)} refused to answer: {reason}",
                            f"{_who(source)} отказался отвечать: {reason}")


async def read_answer_stream(lines: AsyncIterator[str],
                             extract: Callable[[dict], list[tuple[str, str]]],
                             source: str = "") -> AsyncIterator[tuple[str, str]]:
    """Turn an SSE/NDJSON line source into ("content" | "reasoning", text).

    `extract(event)` returns the text pieces one parsed frame carries.

    The stream is never allowed to look complete when it is not: an unreadable
    frame raises (what reached the screen was cut, and the loop drops it and asks
    again), and a stream that carried no text at all raises too, so no provider
    hands the agent an empty string to present as an answer.

    What a line parser cannot see: a connection cut exactly on a line boundary
    reads the same as a finished answer. `[DONE]` is the only word for "that was
    the end", which is why a stream that stops without it is trusted only as far
    as its last readable frame — and why text is never added after the marker.
    """
    got_any = False
    async for payload in sse_payloads(lines):
        if payload.strip() in _DONE_ALIASES:
            return
        try:
            event = json.loads(payload)
        except ValueError as e:
            raise _stream_error(
                f"{_who(source)} sent an answer fragment that cannot be read "
                f"({payload[:80]!r}) — the stream was cut, so what is on screen is "
                f"not the whole answer",
                f"{_who(source)} прислал фрагмент ответа, который не читается "
                f"({payload[:80]!r}) — поток оборвался, поэтому на экране не весь ответ",
            ) from e
        if not isinstance(event, dict):
            continue                        # JSON that is not an event object carries nothing
        for kind, text in extract(event) or ():
            if text:
                got_any = True
                yield (kind, text)
    if not got_any:
        raise _stream_error(f"{_who(source)} closed the stream without an answer",
                            f"{_who(source)} закрыл поток, не отправив ответ")


class BaseProvider(ABC):
    name: str = ""
    models: list[str] = []
    
    @abstractmethod
    async def chat(self, messages: list[dict], model: str = "", stream: bool = False) -> str:
        ...
    
    async def chat_stream(self, messages: list[dict], model: str = "") -> AsyncIterator[tuple[str, str]]:
        """Yield ("content" | "reasoning", text) as the answer arrives.

        The agent loop unpacks pairs, so a provider that yields bare strings
        breaks streaming for that provider — and only for that provider.

        The contract has a second half: a stream that never carried a piece of
        text raises `ProviderStreamError` instead of ending quietly. Returning
        nothing is how a dead endpoint reads as "the model had no answer", and
        both providers answer through the one reader below, so neither gets to
        disagree about it.
        """
        raise NotImplementedError
