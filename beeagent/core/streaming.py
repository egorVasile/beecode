"""The model's mouth: streaming, silence detection and retry.

This used to live inside `Agent` (`core/agent.py`), mixed with provider
selection, tool execution and session bookkeeping. It moves here unchanged
in behaviour: one streamed chunk with an idle budget, prompt-echo detection
for free endpoints that recite the request back, and three attempts with
backoff before giving up.

`Agent` keeps thin wrappers (`_next_token`, `_stream_response`,
`_is_prompt_echo`) so existing callers and tests keep working; the logic
lives in the module functions below.
"""
import asyncio

from beeagent.i18n import L

# How often to reassure the user that a slow endpoint is still being waited on.
HEARTBEAT_SECONDS = 15

# Free endpoints sometimes answer with a copy of the prompt they were sent —
# OpenaiChat in guest mode is the usual culprit. That text is noise on
# screen and poison in history, so it never counts as an answer.
ECHO_MARKERS = ("[SYSTEM: You are", "Guest prompt:", "Do NOT say you lack file access")
# How much prompt text counts as a recital rather than an answer, and how much
# of the reply the copied part has to be for the reply to *be* the copy.
ECHO_MIN_CHARS = 60
ECHO_SHARE = 0.7


def _flat(text) -> str:
    """Whitespace-normalised, lower-cased text: how a copy is compared."""
    return " ".join(str(text if text is not None else "").split()).lower()


def _reason(error, previous) -> str:
    """What to tell the user about a failed attempt, without losing the diagnosis.

    asyncio's own TimeoutError carries no message, so storing the exception as-is
    replaced the descriptive "the endpoint has been silent for N seconds" with an
    empty string and the retry report ended "(last error: )".
    """
    text = str(error).strip()
    if text:
        return text
    known = "" if previous is None else str(previous).strip()
    return known or type(error).__name__


def _no_answer(last_error) -> RuntimeError:
    """An endpoint that answered nothing failed; it never counts as a reply."""
    return RuntimeError(L(
        f"the provider returned an empty answer after all attempts "
        f"(last error: {last_error}) — try again or switch model (/models)",
        f"провайдер вернул пустой ответ после всех попыток "
        f"(последняя ошибка: {last_error}) — попробуй ещё раз или смени модель (/models)",
    ))


def is_prompt_echo(content: str, messages: list[dict]) -> bool:
    """Is this reply the prompt coming back at us, rather than an answer?

    The prompt is the framing we sent: the system header and the rows of the
    conversation. What a tool read back (`role == "tool"`) is not the prompt —
    it is the subject the user asked about, and an answer about a file repeats
    that file almost word for word. Matching the reply against the whole
    request used to discard those answers as echoes, six paid requests per
    question, until the user was told "the provider returned an empty answer".

    And a match has to be a copy, not an overlap: one side has to contain the
    other whole. An answer that opens by restating the question shares text with
    the prompt and still deserves to be shown.
    """
    text = _flat(content)
    if not text:
        return False
    if any(marker in content for marker in ECHO_MARKERS):
        return True
    if len(text) < ECHO_MIN_CHARS:
        return False
    for message in messages:
        role = message.get("role")
        if role == "tool":
            continue            # what a tool read back is history, not the prompt
        sent = _flat(message.get("content"))
        if len(sent) < ECHO_MIN_CHARS:
            continue
        if text in sent:
            return True                        # the reply is prompt and nothing else
        if (role == "system" and sent in text
                and len(sent) >= ECHO_SHARE * len(text)):
            return True                        # the reply recites our own framing
    return False


async def next_token(iterator, idle: int, callback, announce: bool,
                     heartbeat: float = HEARTBEAT_SECONDS):
    """One streamed chunk, or TimeoutError after `idle` seconds of silence.

    Silence is polled in heartbeat slices so the UI can say "still waiting,
    15 s" instead of leaving the user to wonder whether the agent died. The
    pending chunk is deliberately not wrapped in wait_for: cancelling an
    async generator's __anext__ closes the stream and would silently
    truncate the answer.

    The slice never outlasts the budget it is measuring. Fixed 15-second
    slices made `stream_idle_timeout=2` fire after 15 s — and for any budget
    of 15 or less the "waiting" notice could not be reached before the
    timeout, which is the exact case the heartbeat exists to cover.
    """
    task = asyncio.create_task(iterator.__anext__())
    waited = 0
    try:
        while True:
            slice_seconds = min(heartbeat, max(1, idle - waited))
            done, _ = await asyncio.wait({task}, timeout=slice_seconds)
            if done:
                return task.result()          # StopAsyncIteration propagates
            waited += slice_seconds
            if waited >= idle:
                raise asyncio.TimeoutError()
            if callback and announce:
                callback("waiting", {"seconds": waited})
    except BaseException:
        task.cancel()
        raise


async def stream_response(provider, messages, callback, model: str = "",
                          idle: int = 90,
                          heartbeat: float = HEARTBEAT_SECONDS) -> str:
    """Stream the model response with retry.

    Emits "reasoning_delta" for thinking tokens and "stream_delta" for
    answer tokens. Raises only when all attempts fail.

    Every wait on the endpoint is bounded. Free providers routinely accept
    the request, open the stream, and then say nothing at all; without an
    idle timeout the agent just sat there and looked dead to the user.
    """
    idle = max(3, int(idle or 90))
    last_error = None
    for attempt in range(3):
        if callback and attempt > 0:
            callback("retry", {"attempt": attempt + 1})

        if hasattr(provider, "chat_stream"):
            emitted = False
            stream = None
            try:
                answer, reasoning = [], []
                stream = provider.chat_stream(messages, model=model)
                iterator = stream.__aiter__()
                while True:
                    try:
                        kind, text = await next_token(
                            iterator, idle, callback, announce=not emitted,
                            heartbeat=heartbeat)
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise TimeoutError(
                            f"эндпоинт молчит {idle} секунд — вероятна перегрузка провайдера"
                        )
                    if kind == "reasoning":
                        reasoning.append(text)
                        if callback:
                            callback("reasoning_delta", {"text": text})
                    else:
                        answer.append(text)
                        if callback:
                            callback("stream_delta", {"text": text})
                        emitted = True
                content = "".join(answer)
                if not content.strip():
                    last_error = "empty response"
                elif is_prompt_echo(content, messages):
                    last_error = "эндпоинт вернул эхо нашего промпта"
                else:
                    return content
            except asyncio.CancelledError:
                raise
            except Exception as e:
                last_error = _reason(e, last_error)   # fall through to retry
            finally:
                if stream is not None:
                    try:
                        await stream.aclose()
                    except (OSError, RuntimeError):
                        # Aclose on a still-running generator raises
                        # RuntimeError; closing is best-effort either way.
                        # CancelledError is deliberately NOT caught: a
                        # cancel must propagate, never be laundered here.
                        pass
            if emitted and callback:
                # Partial text already reached the UI; have it drop that
                # fragment so the fallback answer prints cleanly.
                callback("stream_reset", {})

        try:
            text = await asyncio.wait_for(provider.chat(messages, model=model), idle * 2)
            if not (text or "").strip():
                last_error = "empty response"
            elif is_prompt_echo(text, messages):
                last_error = "эндпоинт вернул эхо нашего промпта"
            else:
                # Non-stream fallback: the UI never saw this text, so
                # emit it as one delta or the answer is silently lost.
                if callback:
                    callback("stream_delta", {"text": text})
                return text
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            last_error = f"non-stream chat timed out after {idle * 2}s"
        except Exception as e:
            last_error = _reason(e, last_error)

        await asyncio.sleep(1.5 * (attempt + 1))  # backoff

    raise _no_answer(last_error)
