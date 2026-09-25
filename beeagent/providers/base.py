"""What every provider owes the user: the whole answer, or the reason it is not there.

The functions here are the one place a wire full of frames becomes text on
screen. `sse_payloads` assembles events, `read_answer_stream` turns them into
answer pieces and refuses to call a broken stream an answer, and the model-list
helpers say where a list of names came from — from the endpoint, or from the
package. A provider that reads its lines itself gets all of that wrong in its own
private way, which is how a half answer reaches a user as a complete one.
"""
from abc import ABC, abstractmethod
from typing import AsyncIterator, Callable
import json
import re

DONE_SENTINEL = "[DONE]"
_DONE_ALIASES = (DONE_SENTINEL, '"[DONE]"')

# Where a list of model names came from. Three of these are a fallback, and a
# fallback that is not named is a lie: a person whose seat was revoked must not
# be shown a model menu that looks like it came from a live pool.
LIST_LIVE = "live"                     # the endpoint itself answered with this list
LIST_SHIPPED = "shipped"               # the list that ships with BeeCode; never asked
LIST_REFUSED = "refused"               # the endpoint answered and refused us (401/403)
LIST_UNREACHABLE = "unreachable"       # nothing answered at all
LIST_NO_SEAT = "no-seat"               # a pool seat this install does not have yet
LIST_SEAT_REFUSED = "seat-refused"     # the pool said no to this seat


class ProviderStreamError(RuntimeError):
    """A stream that cannot honestly be reported as an answer.

    Two shapes, and they are the same failure from the user's side: the endpoint
    closed without sending any text, and a frame arrived torn so the text on
    screen is only part of the answer.
    """

    # Why this stream failed: "torn" (a frame could not be read), "empty" (no text
    # at all — a retry may honestly ask again) or "refused" (the endpoint said no).
    code = ""


class ProviderAPIError(RuntimeError):
    """The endpoint answered with a status and, nearly always, a reason.

    `raise_for_status()` keeps the status and throws the reason away, which turns
    "no such model" into "Client error '404 Not Found' for url". The reason is the
    only part the user can act on, so it goes into the message.
    """

    def __init__(self, message: str, status: int = 0, retry_after: int = 0):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


def _who(source: str) -> str:
    """The endpoint, named — in the language the user is reading in."""
    from beeagent.i18n import L
    return source or L("the endpoint", "эндпоинт")


def _stream_error(english: str, russian: str, code: str = "",
                  exc: type = ProviderStreamError) -> ProviderStreamError:
    from beeagent.i18n import L
    error = exc(L(english, russian))
    error.code = code
    return error


def _is_record(text: str) -> bool:
    """Is this `data:` value a whole JSON record on its own?

    The question decides whether a blank line may be trusted to arrive. It can't:
    our own pool server forwards the upstream's `data:` lines and drops the
    separator between them, so a live streamed answer reaches the client as
    records one after another. Joined, they are one unparseable payload; read as
    they are written, they are the answer.
    """
    stripped = (text or "").strip()
    if not stripped.startswith(("{", "[")):
        return False
    try:
        json.loads(stripped)
    except ValueError:
        return False
    return True


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
    * so is a `data:` line that is a whole JSON record on its own. Our own pool
      server forwards the upstream's `data:` lines and drops the blank line
      between them, and an answer streamed through it is exactly that shape: a
      record per line with no separator. Insisting on the separator here would
      glue every frame of a live answer into one unparseable payload;
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
            if not pending and _is_record(value):
                yield value                     # a whole frame with no blank line after it
                continue
            pending.append(value)           # keep the spacing: the answer is in it
            continue
        if text.startswith(("{", "[")):
            for payload in closed():
                yield payload
            yield line                      # one JSON record per line
    for payload in closed():
        yield payload                       # an event the stream ended on, no blank line


def check_error_frame(event: dict, source: str = "",
                      exc: type = ProviderStreamError) -> None:
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
                            f"{_who(source)} отказался отвечать: {reason}",
                            code="refused", exc=exc)


async def read_answer_stream(lines: AsyncIterator[str],
                             extract: Callable[[dict], list[tuple[str, str]]],
                             source: str = "",
                             exc: type = ProviderStreamError) -> AsyncIterator[tuple[str, str]]:
    """Turn an SSE/NDJSON line source into ("content" | "reasoning", text).

    `extract(event)` returns the text pieces one parsed frame carries — that is
    the only thing a provider has to know about its own frame shape, and it is
    why four providers with three different wire dialects share this one reader.

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
                code="torn", exc=exc,
            ) from e
        if not isinstance(event, dict):
            continue                        # JSON that is not an event object carries nothing
        for kind, text in extract(event) or ():
            if text:
                got_any = True
                yield (kind, text)
    if not got_any:
        raise _stream_error(f"{_who(source)} closed the stream without an answer",
                            f"{_who(source)} закрыл поток, не отправив ответ",
                            code="empty", exc=exc)


# --- an endpoint's own words, kept ------------------------------------------

_TAG = re.compile(r"<[^>]+>")


def _flat(text: str, limit: int = 200) -> str:
    """One line, no longer than a sentence: what fits in an error message."""
    return " ".join((text or "").split())[:limit]


def body_reason(text: str) -> str:
    """The explanation inside a response body, JSON or HTML.

    A CDN error page and an OpenAI error envelope both carry the reason a request
    failed; they differ only in how it has to be gotten out. Returning "" means
    the body truly had nothing to say, and the caller says so instead.
    """
    stripped = (text or "").strip()
    if not stripped:
        return ""
    try:
        body = json.loads(stripped)
    except ValueError:
        body = None
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str) and error.strip():
            return _flat(error)
        if isinstance(error, dict):
            for field in ("message", "type", "code"):
                value = str(error.get(field) or "").strip()
                if value and value not in ("null", "None"):
                    return _flat(value)
        for field in ("message", "detail", "error_description", "reason"):
            value = body.get(field)
            if isinstance(value, str) and value.strip():
                return _flat(value)
        return _flat(stripped)
    if "<" in stripped:                       # an HTML page from a gateway or CDN
        cleaned = re.sub(r"(?is)<(script|style).*?</\1>", " ", stripped)
        return _flat(_TAG.sub(" ", cleaned)) or _flat(stripped)
    return _flat(stripped)


def api_error(status: int, text: str, source: str = "") -> ProviderAPIError:
    """A non-2xx answer, in the endpoint's words rather than in httpx's."""
    from beeagent.i18n import L
    reason = body_reason(text)
    if reason:
        return ProviderAPIError(L(f"{_who(source)} answered {status}: {reason}",
                                  f"{_who(source)} ответил {status}: {reason}"),
                                status=status)
    return ProviderAPIError(L(f"{_who(source)} answered {status} and sent no explanation",
                              f"{_who(source)} ответил {status} и не пояснил ничего"),
                            status=status)


async def raise_for_api_status(response, source: str = "") -> None:
    """`raise_for_status()` that keeps the body — the reason is in the body.

    On a streamed response the body has not been read yet, so it is read here;
    an error is the one case where the whole payload is small and wanted.
    """
    status = getattr(response, "status_code", 0) or 0
    if status < 400:
        return
    text = ""
    try:
        read = getattr(response, "aread", None)
        if read is not None and not getattr(response, "is_stream_consumed", True):
            await read()
        text = response.text or ""
    except Exception:                     # noqa: BLE001 — no body is not a crash
        text = ""
    raise api_error(status, text, source)


def parse_json_body(text: str, source: str = "") -> dict:
    """The JSON an endpoint was asked for, or a sentence about what came instead.

    A gateway in front of a provider answers 200 with an HTML page often enough
    that "Expecting value: line 1 column 1" cannot be left as the diagnosis: it
    names our parser instead of the thing that happened.
    """
    from beeagent.i18n import L
    try:
        body = json.loads((text or "").strip() or "null")
    except ValueError as e:
        reason = body_reason(text) or _flat(text or "", 120)
        raise ProviderAPIError(L(
            f"{_who(source)} sent something that is not JSON"
            f"{f': {reason}' if reason else ''} — an error page wearing a 200",
            f"{_who(source)} прислал не JSON"
            f"{f': {reason}' if reason else ''} — страница ошибки под видом ответа",
        )) from e
    return body if isinstance(body, dict) else {}


def transport_error(error: Exception, source: str = "", idle: float = 0.0) -> RuntimeError:
    """A connection failure in words, because httpx's own are often empty.

    `str(httpx.ReadTimeout(""))` is the empty string, and the retry report ended
    "(last error: )": a silent endpoint was reported as no endpoint at all. The
    caller raises this — `raise transport_error(e, …) from e` — so the httpx
    traceback is still behind the sentence.
    """
    from beeagent.i18n import L
    name = error.__class__.__name__
    detail = _flat(str(error), 120)
    if name == "ConnectTimeout":
        return ProviderAPIError(L(f"{_who(source)} did not accept the connection "
                                  f"within {int(idle)} s",
                                  f"{_who(source)} не принял соединение за {int(idle)} с"))
    if name in ("ReadTimeout", "WriteTimeout", "PoolTimeout"):
        return ProviderAPIError(L(
            f"{_who(source)} went silent and did not answer within {int(idle)} s"
            f"{f' ({detail})' if detail else ''} — a slow model is not a broken one, "
            f"and this attempt can be asked again",
            f"{_who(source)} молчит и не ответил за {int(idle)} с"
            f"{f' ({detail})' if detail else ''} — медленная модель это не сломанная, "
            f"попытку можно повторить")
        )
    return ProviderAPIError(L(f"{_who(source)} is not reachable: "
                              f"{detail or 'the connection failed'}",
                              f"{_who(source)} недоступен: {detail or 'соединение не установлено'}"))


def default_idle_timeout() -> float:
    """The agent's own silence budget, so a provider never quits before it.

    `stream_idle_timeout` is how long the agent is prepared to wait before it
    explains a stall; a read timeout shorter than that kills a slow model first
    and the user reads a bare timeout as a broken provider. Config is read here
    rather than passed in because a provider built by hand (a `custom_providers`
    entry, a plugin) has nobody to pass it.
    """
    try:
        from beeagent.config.schema import BeeConfig
        return float(max(10, int(BeeConfig().stream_idle_timeout or 90)))
    except Exception:                   # noqa: BLE001 — a broken config is not a crash
        return 90.0


# --- the catalogue a provider reports ---------------------------------------

class ModelCatalog:
    """`models`, answered two ways: what ships, and what the endpoint serves.

    Routing read the list compiled into the package while the picker showed the
    list the endpoint had just given, so a model the user picked — `llama-4-scout`
    in the measured case — was replaced by the first shipped name and
    `config.model` was rewritten to match. Reading the same attribute from both
    sides is only honest if it answers with what the endpoint said.

    On the class it is the shipped list, which is what the picker falls back on
    when the endpoint cannot be reached; on an instance it is the live list as
    soon as the endpoint has answered for one.
    """

    def __init__(self, declared=()):
        self.declared = [str(model) for model in (declared or ()) if str(model).strip()]

    def __set_name__(self, owner, name):
        self.attr = name

    def _declared_for(self, obj) -> list[str]:
        stored = obj.__dict__.get("_declared_" + getattr(self, "attr", "models"))
        return [str(m) for m in stored if str(m).strip()] if stored is not None \
            else list(self.declared)

    def __get__(self, obj, objtype=None):
        if obj is None:
            return list(self.declared)          # the class: what ships with BeeCode
        live = obj.__dict__.get("_live_models") or []
        if live:
            return [str(m) for m in live]
        return self._declared_for(obj)

    def __set__(self, obj, value):
        # `self.models = (...)` in a constructor: the names this endpoint is known
        # for, before anybody has asked it.
        obj.__dict__["_declared_" + getattr(self, "attr", "models")] = \
            [str(m) for m in (value or ()) if str(m).strip()]


class BaseProvider(ABC):
    name: str = ""
    # The shipped list plus, once read, the live one. See `ModelCatalog`.
    models: ModelCatalog = ModelCatalog()
    # What the endpoint itself answered for the last time it was asked; empty = not asked.
    _live_models: list[str] = []
    # Where the list of model names came from: one of the LIST_* codes above.
    model_list_state: str = LIST_SHIPPED
    model_list_reason: str = ""
    # How `model_list_note` names the thing it asked for a catalogue.
    list_source: tuple[str, str] = ("the endpoint", "эндпоинт")

    @property
    def live_models(self) -> list[str]:
        """The catalogue the endpoint named last, or nothing if it never did."""
        return list(getattr(self, "_live_models", None) or [])

    def remember_live_models(self, names) -> list[str]:
        """Write down what the endpoint said it serves, and say that it said so."""
        found = []
        for name in names or ():
            model = str(name or "").strip()
            if model and model not in found:
                found.append(model)
        self._live_models = found
        if found:
            self.model_list_state = LIST_LIVE
            self.model_list_reason = ""
        return found

    def report_model_list_failure(self, state: str, reason: str = "") -> str:
        """Record that the list on screen did not come from the endpoint."""
        self.model_list_state = state
        self.model_list_reason = _flat(reason, 200)
        return state

    def serves(self, model: str) -> bool:
        """Will this endpoint answer for `model`?

        The question routing needs before it rewrites a user's choice: the live
        list decides when there is one, the shipped list only when there is not.
        A provider that lists no models at all takes any name, because there is
        nothing here to contradict the caller.
        """
        wanted = (model or "").strip()
        if not wanted:
            return False
        known = list(self.models or [])
        if not known:
            return True
        return wanted in known

    def source_name(self) -> str:
        """The provider, named in the language the user is reading in.

        Every sentence the reader raises starts with it, so it has to be a word
        in that language too: "the pool закрыл поток" is neither.
        """
        from beeagent.i18n import L
        english, russian = self.list_source
        return L(english, russian) if english != russian else english

    def model_list_note(self) -> str:
        """One line for the UI: where the model list the user is looking at came from.

        The fallback lists are deliberate — crax's Cloudflare blocks some
        networks outright — but a fallback nobody names looks like a live answer,
        and that is the difference between a user waiting for the box to wake and
        a user changing provider for no reason.
        """
        from beeagent.i18n import L
        who_en, who_ru = self.list_source
        state = self.model_list_state
        detail = self.model_list_reason
        who = L(who_en, who_ru)
        if state == LIST_LIVE:
            return L(f"{who_en} answers for this list of models, read just now",
                     f"{who_ru} отвечает за этот список моделей, прочитан сейчас")
        if state == LIST_UNREACHABLE:
            return L(f"{who} did not answer{f' ({detail})' if detail else ''} — showing the "
                     f"model list that ships with BeeCode, which is not a live one",
                     f"{who} не ответил{f' ({detail})' if detail else ''} — показан список "
                     f"моделей, поставляемый с BeeCode, он не актуальный")
        if state in (LIST_REFUSED, LIST_SEAT_REFUSED, LIST_NO_SEAT):
            return L(f"{who} refused this install{f' ({detail})' if detail else ''} — showing "
                     f"the model list that ships with BeeCode, which is not a live one",
                     f"{who} отклонил эту установку{f' ({detail})' if detail else ''} — показан "
                     f"список моделей, поставляемый с BeeCode, он не актуальный")
        return L(f"{who_en} was never asked — this is the model list that ships with BeeCode",
                 f"{who_ru} никто не спрашивал — это список моделей, поставляемый с BeeCode")

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
        every provider answers through `read_answer_stream`, so none gets to
        disagree about it.
        """
        raise NotImplementedError
