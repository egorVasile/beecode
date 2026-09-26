"""BeeCode core agent with realtime streaming and tool feedback loop.

The agent loop:
1. Deliver any pending user messages queued while the agent was busy.
2. Build messages (system prompt + tool catalog + history).
3. Stream the model response token-by-token to the UI.
4. Parse tool calls from the streamed text.
5. Execute tools, feed results back as [tool result] messages.
6. Repeat until the model replies with plain text (task done).
"""
import asyncio
import json
import re

from beeagent.i18n import L
from beeagent.config.loader import load_config
from beeagent.config.schema import BeeConfig
from beeagent.providers.registry import ProviderRegistry
from beeagent.providers.g4f_provider import G4fProvider
from beeagent.providers.ollama import OllamaProvider
from beeagent.providers.openai_compat import OpenAICompatProvider
from beeagent.tools.registry import ToolRegistry
from beeagent.tools.base import ToolResult
from beeagent.tools.read import ReadTool
from beeagent.tools.write import WriteTool
from beeagent.tools.edit import EditTool
from beeagent.tools.bash import BashTool
from beeagent.tools.grep import GrepTool
from beeagent.tools.glob_tool import GlobTool
from beeagent.tools.diagnostics import DiagnosticsTool
from beeagent.tools.diagram import DiagramTool
from beeagent.tools.files import MoveTool, RemoveTool
from beeagent.tools.patch import PatchTool
from beeagent.tools.list_dir import ListDirectoryTool
from beeagent.tools.web_fetch import WebFetchTool
from beeagent.tools.web_search import WebSearchTool
from beeagent.tools.git import GitTool
from beeagent.tools.todo import TodoTool
from beeagent.core.queue import PendingQueue
from beeagent.core.session import Session
from beeagent.core.context import ContextManager
from beeagent.core.economy import EconomyManager
from beeagent.core.permissions import Permissions
from beeagent.core import windows
from beeagent.core.parser import CommandParser, ParsedCommand, ParsedResponse


# How often to reassure the user that a slow endpoint is still being waited on.
HEARTBEAT_SECONDS = 15

# A model that writes "now I will read the file" and stops is mid-task, not
# finished — measured live on 2026-09-21, where such a reply ended the run and
# the file was never opened. One nudge sends it the reminder; a second would be
# a loop waiting to happen, so the answer stands after one.
_PROMISE_TO_ACT = re.compile(
    r"(сейчас|сначала|затем|потом|давайте|позвольте)\D{0,40}"
    r"(прочита|прочту|посмотрю|проверю|открою|найду|создам|запишу|запущу|изучу|посчитаю|выполню)"
    r"|\b(i|we|let me|let's|i'll|now)\b\s*\w{0,10}\s*"
    r"(read|check|look|open|find|create|write|run|inspect|search|list|count)\b",
    re.I,
)
ACT_NOW = ("[SYSTEM: you described a next step but sent no tool call, so nothing ran. "
           "Either send the ```json {\"tool\": ..., \"args\": {...}}``` block now, "
           "or answer without promising to act.]")

# Sent back when a call was recognised but could not be read. It goes into the
# transcript, not just the request: an attempt really was made and refused.
_RESEND_CALL = ("[BeeCode] Your tool call arrived broken, so it was not run: {note}. "
                "Send it again in one ```json block, with every value on one line — "
                "write a newline inside a string as \\n, a backslash as \\\\ and a "
                "quote as \\\". If the content is long, write the first part with "
                "`write` and add the rest with `edit`.")

# How many turns in a row we re-ask a model whose call keeps arriving cut off.
# One retry is the model's chance; a second is the loop paying for itself.
BROKEN_CALL_CHANCES = 2


def _gave_up_on_calls() -> str:
    """What to say when the model's calls keep arriving unreadable."""
    return L(
        "Stopping: the model sent a tool call that never arrived complete twice in "
        "a row and nothing ran, so there is no answer to show. Ask again, or switch "
        "model (/models).",
        "Останавливаюсь: модель дважды подряд присылала вызов, который не доехал "
        "целиком, и ничего не выполнилось — ответа нет. Спроси ещё раз или смени "
        "модель (/models).")


# What a cancelled turn knows about a tool it stopped waiting for. The worker
# thread runs to completion whatever we do here (asyncio cannot kill it), so the
# one sentence that is true is that the result was abandoned, not the work.
_ABANDONED = ("abandoned: the user cancelled this answer, so its result was never "
              "read. The tool had already started and may still have finished its "
              "work — check what it changed before doing it again.")


def _as_tool_result(raw) -> ToolResult:
    """Fit whatever an extension returned into the one shape the loop reads.

    A plugin or MCP tool answering with a bare string (the commonest authoring
    mistake) has still run the call: that is a result in the wrong wrapper, not a
    crash. Reading `.output` off it outside the try used to end run() with an
    AttributeError after the assistant row carrying the call was already written,
    which left the saved transcript ending on a call with no result.
    """
    if isinstance(raw, ToolResult):
        return raw
    if raw is None:
        return ToolResult(output="", error=False)
    if isinstance(raw, str):
        return ToolResult(output=raw, error=False)
    output = getattr(raw, "output", None)      # a duck-typed ToolResult of ours
    if output is not None:
        return ToolResult(output=str(output), error=bool(getattr(raw, "error", False)))
    if isinstance(raw, (dict, list, tuple)):
        return ToolResult(output=json.dumps(raw, ensure_ascii=False), error=False)
    return ToolResult(output=str(raw), error=False)


def _flat(text) -> str:
    """Whitespace-normalised, lower-cased text: how a copy is compared."""
    return " ".join(str(text if text is not None else "").split()).lower()


def _carries_a_call(parser, text: str) -> bool:
    """Whether this reply is a step of the loop rather than an answer.

    A cached reply that names a tool — even one whose payload arrived cut off —
    must not be served from the cache: it would print and run nothing.
    """
    parsed = parser.parse(text)
    return parsed.has_commands or bool(parsed.dropped)


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


class Agent:
    def __init__(self, config: BeeConfig = None, workdir: str = "."):
        self.config = config or load_config(workdir)
        self.workdir = workdir

        self.providers = ProviderRegistry()
        self.providers.register(G4fProvider())
        self.provider_errors: list[str] = []
        # Providers the user configured in beeagent.json; without this they are
        # advertised by /providers but /provider <name> silently fell back to g4f.
        for custom in self.config.custom_providers:
            try:
                if custom.type == "ollama":
                    self.providers.register(OllamaProvider(base_url=custom.url, model=custom.model))
                elif custom.type == "openai_compat":
                    self.providers.register(OpenAICompatProvider(
                        base_url=custom.url, api_key=custom.key or "", model=custom.model))
            except Exception as e:
                self.provider_errors.append(f"{custom.name}: {e}")

        # Free-tier endpoints the user signed up for themselves. Registered only
        # when a key exists, so /providers can show what is ready to use.
        from beeagent.providers.presets import ENDPOINTS, key_for
        self.ready_presets: list[str] = []
        # The UI installs this to ask the user what to do about a rate limit;
        # with no UI around, the answer is "wait it out".
        self.route_question = None
        for endpoint in ENDPOINTS:
            key = key_for(endpoint, self.config.api_keys)
            if key:
                self.attach_preset(endpoint.name, key)

        # A pool the operator runs. Registered as soon as its address is known —
        # with no seat yet the provider answers "run /pool enroll" instead of
        # "unknown provider", and neither the address nor the seat is a key.
        from beeagent.providers.pool import PoolProvider
        self.providers.register(PoolProvider(
            url=self.config.pool_url, token=self.config.pool_token,
            idle_timeout=max(10, int(self.config.stream_idle_timeout or 90))))

        self.tools = ToolRegistry()
        for tool_cls in [ReadTool, WriteTool, EditTool, BashTool,
                         GrepTool, GlobTool, ListDirectoryTool, WebSearchTool,
                         WebFetchTool, GitTool, TodoTool, DiagramTool,
                         DiagnosticsTool, PatchTool, MoveTool, RemoveTool]:
            self.tools.register(tool_cls())

        self.economy = EconomyManager(
            mode=self.config.mode,
            cache_dir=self.config.economy.cache_dir,
            cache_enabled=self.config.economy.cache_enabled,
            cache_ttl_minutes=self.config.economy.cache_ttl_minutes,
        )

        # Tools that change the machine need a grant; see core/permissions.py.
        self.permissions = Permissions(
            mode=self.config.permissions.mode,
            allowed=self.config.permissions.allowed,
        )

        self.context = ContextManager(
            model=self.config.model, window=self.config.max_context_tokens or None)
        # A measured window belongs to the endpoint that measured it, and
        # ContextManager asks for a window by model name alone — so the name the
        # reads should resolve against is set here and refreshed on every run.
        windows.note_provider(self.config.provider or "")
        # A model name saved yesterday can be one the endpoint stopped serving
        # today — crax rewrote its catalogue on 2026-09-26 and twelve of the
        # thirteen names it had offered ceased to exist — and the answer to the
        # stale name is a 400 the user reads as "the pool is broken". Fix the pair
        # at the seam where both halves are known, and say what was changed.
        self.startup_notes = self._reconcile_model_with_provider()
        self.parser = CommandParser()

        # Skills, plugin tool packs, and MCP servers installed from the catalog.
        from beeagent.plugins.loader import PluginLoader
        self.plugins = PluginLoader(self, gate_project=True)
        try:
            self.plugins.load_all()
        except Exception:
            # A broken extension must never take the agent down.
            self.plugins.load_errors.append("plugin loader failed")

        # Messages typed while the agent is busy; delivered with the next
        # model call so nothing the user says is lost.
        self.pending = PendingQueue()
        self.is_busy = False
        # Set by /stop. A blocking read from the model cannot be interrupted from
        # Python without killing the thread, so this ends the loop at the next
        # turn instead of pretending to cut the request mid-byte.
        self.stop_requested = False

    def request_stop(self) -> bool:
        """Ask the running turn to end. True if something was actually running."""
        # Arming an idle agent is NOT the race window: is_busy is the only honest
        # "a turn exists to stop" — the REPL sets it synchronously before the task
        # even exists (ui/repl.py:336/353), so busy-then-stop is a real turn already
        # awaited and must arm, while a False is_busy means there is nothing to stop
        # and the flag would only wait there to eat the user's next question.
        if not self.is_busy:
            return False
        self.stop_requested = True
        return True

    def sync_config_permissions(self):
        """Mirror the live gate into the config so a save cannot undo it."""
        self.config.permissions.mode = self.permissions.mode
        self.config.permissions.allowed = sorted(self.permissions.granted)

    def reload_extensions(self) -> list[str]:
        """Re-scan installed skills, plugin packs and MCP servers after a change."""
        self.plugins.reset()
        try:
            self.plugins.load_all()
        except Exception as e:
            self.plugins.load_errors.append(f"plugin loader failed: {e}")
        self.context.skills_section = self.plugins.skills_prompt_section()
        return self.plugins.load_errors

    # Free endpoints sometimes answer with a copy of the prompt they were sent
    # — OpenaiChat in guest mode is the usual culprit. That text is noise on
    # screen and poison in history, so it never counts as an answer.
    ECHO_MARKERS = ("[SYSTEM: You are", "Guest prompt:", "Do NOT say you lack file access")
    # How much prompt text counts as a recital rather than an answer, and how much
    # of the reply the copied part has to be for the reply to *be* the copy.
    ECHO_MIN_CHARS = 60
    ECHO_SHARE = 0.7

    def _is_prompt_echo(self, content: str, messages: list[dict]) -> bool:
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
        if any(marker in content for marker in self.ECHO_MARKERS):
            return True
        if len(text) < self.ECHO_MIN_CHARS:
            return False
        for message in messages:
            role = message.get("role")
            if role == "tool":
                continue            # what a tool read back is history, not the prompt
            sent = _flat(message.get("content"))
            if len(sent) < self.ECHO_MIN_CHARS:
                continue
            if text in sent:
                return True                        # the reply is prompt and nothing else
            if (role == "system" and sent in text
                    and len(sent) >= self.ECHO_SHARE * len(text)):
                return True                        # the reply recites our own framing
        return False

    async def _next_token(self, iterator, idle: int, callback, announce: bool):
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
                slice_seconds = min(HEARTBEAT_SECONDS, max(1, idle - waited))
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

    async def _stream_response(self, provider, messages, callback, model: str = "") -> str:
        """Stream the model response with retry.

        Emits "reasoning_delta" for thinking tokens and "stream_delta" for
        answer tokens. Raises only when all attempts fail.

        Every wait on the endpoint is bounded. Free providers routinely accept
        the request, open the stream, and then say nothing at all; without an
        idle timeout the agent just sat there and looked dead to the user.
        """
        model = model or self.config.model
        idle = max(3, int(self.config.stream_idle_timeout or 90))
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
                            kind, text = await self._next_token(
                                iterator, idle, callback, announce=not emitted)
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
                    elif self._is_prompt_echo(content, messages):
                        last_error = "эндпоинт вернул эхо нашего промпта"
                    else:
                        return content
                except Exception as e:
                    last_error = _reason(e, last_error)   # fall through to retry
                finally:
                    if stream is not None:
                        try:
                            await stream.aclose()
                        except Exception:
                            pass
                if emitted and callback:
                    # Partial text already reached the UI; have it drop that
                    # fragment so the fallback answer prints cleanly.
                    callback("stream_reset", {})

            try:
                text = await asyncio.wait_for(provider.chat(messages, model=model), idle * 2)
                if not (text or "").strip():
                    last_error = "empty response"
                elif self._is_prompt_echo(text, messages):
                    last_error = "эндпоинт вернул эхо нашего промпта"
                else:
                    # Non-stream fallback: the UI never saw this text, so
                    # emit it as one delta or the answer is silently lost.
                    if callback:
                        callback("stream_delta", {"text": text})
                    return text
            except Exception as e:
                last_error = _reason(e, last_error)

            await asyncio.sleep(1.5 * (attempt + 1))  # backoff

        raise _no_answer(last_error)

    def _model_for(self, provider, callback=None) -> str:
        """The model id to actually send.

        `config.model` is one global string while every provider has its own
        catalogue, so a groq key pointed at "gpt-4" only ever answers 404. A
        provider that lists its models gets the request corrected to one of
        them; g4f routes any name it advertises, so it is left alone.
        """
        wanted = self.config.model or ""
        known = list(getattr(provider, "models", None) or [])
        if not known or wanted in known:
            return wanted or getattr(provider, "default_model", "")
        if callable(getattr(provider, "discover_models", None)):
            return wanted
        model = known[0]
        # Deliberate, documented and announced: `/provider groq` with gpt-4 in
        # config would otherwise 404 forever. The UI prints the substitution
        # ("this provider has no “gpt-4” — answering with …"), so the change is
        # never silent.
        self.config.model = model
        self.context.model = model
        if callback:
            callback("model_switched", {"from": wanted, "to": model})
        return model

    @staticmethod
    def _g4f_installed() -> bool:
        """Is g4f importable here at all — without importing it."""
        import importlib.util
        try:
            return importlib.util.find_spec("g4f") is not None
        except (ImportError, ValueError):
            return False

    def _provider_or_pool(self, callback):
        """The configured provider, unless it cannot exist on this machine.

        g4f is pure Python, but it declares pycryptodome and brotli, which have no
        Android wheel and no pure-Python fallback, so `pip install beecode` leaves
        g4f out on a phone (see the Termux section of the README for the recipe
        that installs it anyway). Without it the default provider is a dead end:
        the first question would come back "Error calling provider" and the person
        would conclude BeeCode is
        broken. The pool is the other half of the same promise — an answer without
        owning a key — so it takes over, out loud.
        """
        name = self.config.provider or "g4f"
        if name != "g4f" or self._g4f_installed():
            return self.providers.select(name)
        pool = self.providers.get("pool")
        if pool is None or not getattr(pool, "url", ""):
            return self.providers.select(name)
        self.config.provider = "pool"
        if callback:
            callback("provider_fallback", {"from": "g4f", "to": "pool",
                                           "seat": bool(pool.token)})
        return pool

    def _reconcile_model_with_provider(self) -> str:
        """Move the session onto a model its endpoint actually answers for.

        Only when the provider has a list to check against and the saved name is
        not in it: an empty list means the endpoint has not been asked yet, and a
        made-up default is worse than the stale name plus a real error. The note
        is returned rather than printed, because the agent is built before anything
        is on screen — both interfaces show it, and a silent swap reads as BeeCode
        ignoring a setting.
        """
        from beeagent.i18n import L

        provider = self.providers.get(self.config.provider or "g4f")
        offered = [str(m) for m in (getattr(provider, "models", None) or [])]
        wanted = str(self.config.model or "").strip()
        if provider is None or not offered or not wanted or wanted in offered:
            return ""
        self.config.model = offered[0]
        self.context.model = offered[0]
        return L(f"model “{wanted}” is not one {self.config.provider} answers for — "
                 f"asking {offered[0]} instead; /models lists the rest",
                 f"модель “{wanted}” среди тех, что {self.config.provider} не отвечает — "
                 f"спрашиваю {offered[0]}; остальное в /models")

    def preset_provider(self, name: str, key: str):
        """Build the provider for a preset endpoint, with the class that fits it.

        One factory, because `/key <name>` used to build a generic OpenAI client
        for every name — which replaced crax's provider (two kinds of 429, key
        rotation, the question it asks) with a plain one, and sent a comma-separated
        list of keys as a single bearer token.
        """
        from beeagent.providers.presets import BY_NAME
        endpoint = BY_NAME.get(name)
        if endpoint is None:
            return None
        idle = max(10, int(self.config.stream_idle_timeout or 90))
        if name == "crax":
            from beeagent.providers.crax import CraxProvider
            return CraxProvider(api_key=key, base_url=endpoint.url, idle_timeout=idle,
                                ask=self._ask_route)
        from beeagent.providers.openai_compat import OpenAICompatProvider
        return OpenAICompatProvider(base_url=endpoint.url, api_key=key,
                                    model=endpoint.models[0] if endpoint.models else "gpt-4",
                                    name=name, models=endpoint.models)

    def attach_preset(self, name: str, key: str) -> bool:
        """Register (or re-register) a preset endpoint after a key change."""
        provider = self.preset_provider(name, key)
        if provider is None:
            return False
        self.providers.register(provider, replace=True)
        if name not in self.ready_presets:
            self.ready_presets.append(name)
        return True

    async def _ask_route(self, error):
        """Hand a rate limit to whoever is showing the interface.

        With no UI around — one-shot mode, a plugin driving the agent — there is
        nobody to ask, so the only safe answer is to wait out what the endpoint
        said and try again.
        """
        if self.route_question is None:
            return "wait" if getattr(error, "retry_after", 0) else None
        try:
            return await self.route_question(error)
        except Exception:
            return "wait"

    async def run(self, user_input: str, session: Session = None, callback=None) -> str:
        session = session or Session()
        session.add_user_message(user_input)

        # is_busy is set before anything can raise: an unconfigured provider
        # used to leave it True forever, and then the REPL parked every later
        # message in agent.pending and never ran a single one.
        self.is_busy = True
        # `stop_requested` is deliberately NOT cleared here. The REPL marks itself
        # busy and only then creates the task, so a /stop typed inside that window
        # used to be erased before turn one: request_stop() answered True, the user
        # was told it was stopping, and the whole answer streamed anyway. The flag
        # is cleared in the finally, when this run is genuinely over.
        self.permissions.denied_this_run.clear()
        # (index in the transcript, tool names) of the assistant row whose calls
        # still owe a result. Defined before anything can be cancelled, because the
        # cancel handler below is what reads it.
        call_row = None

        try:
            provider = self._provider_or_pool(callback)
            model = self._model_for(provider, callback)
            # The context window is measured per endpoint, so reading it back has
            # to know which endpoint is answering (see core/windows.py).
            windows.note_provider(getattr(provider, "name", "") or self.config.provider)
            trim_reported = False
            nudged = False
            nudge_pending = False
            broken_streak = 0

            # A zero (or a negative, from a hand-edited beeagent.json) used to end
            # the run before the first request and still report "Max turns reached
            # without a final answer" — an answer was never even asked for.
            for turn in range(max(1, int(self.config.max_turns or 0))):
                if self.stop_requested:
                    # Reported, not swallowed: the user learns the answer was cut
                    # short rather than finished. The finally clears the flag.
                    if callback:
                        callback("stopped", {"turn": turn})
                    return "Stopped by you"

                # 1. Deliver messages the user typed while we were busy:
                #    they go along with this step (+ the system prompt is
                #    rebuilt every turn, so tool instructions are intact).
                pending = self.pending.drain()
                if pending:
                    for item in pending:
                        session.add_user_message(item)
                    if callback:
                        callback("queued_sent", {"items": pending})

                # 2. Fun pending state while the request travels to the model.
                if callback:
                    callback("status", {})

                tool_schemas = self.tools.to_schemas()
                self.context.skills_section = self.plugins.skills_prompt_section()
                self.context.permissions_section = self.permissions.prompt_section(self.tools)
                # A provider that takes the calls natively gets the schemas in the
                # request's `tools` field; repeating them as prose in the system
                # prompt is the single biggest thing we spend tokens on.
                native = (bool(getattr(provider, "supports_tools", False))
                          and self.config.native_tools)
                messages = self.context.build_messages(session.to_dicts(), tool_schemas,
                                                        native=native)
                if nudge_pending:
                    # The reminder rides on this request only: history stays the
                    # conversation the user actually had.
                    messages = messages + [{"role": "user", "content": ACT_NOW}]
                    nudge_pending = False

                prompt_str = json.dumps(messages)
                if self.context.trimmed and not trim_reported:
                    # Tell the user why the model may look forgetful this turn.
                    trim_reported = True
                    if callback:
                        callback("context_trimmed", {"dropped": self.context.trimmed})
                cached = self.economy.check_cache(prompt_str, model)
                if cached and not _carries_a_call(self.parser, cached):
                    if callback:
                        callback("economy_hit", {})
                        callback("response", {"text": cached})
                    # The turn has to land in the transcript: without it the
                    # saved session ends on an unanswered user message, and every
                    # later turn answers the same question again.
                    session.add_assistant_message(cached)
                    return cached
                # A reply that carries a tool call is one step of the loop, not
                # an answer — serving it from cache would print JSON and run
                # nothing.

                try:
                    if native:
                        answer = await provider.complete(messages, model, tool_schemas)
                        response = answer.get("text") or ""
                        calls = answer.get("tool_calls") or []
                        if not calls and not response.strip():
                            # The streamed path refuses an empty answer; this one
                            # used to hand it back as a finished reply — it reached
                            # "done", was written into the transcript and cached,
                            # and the user watched a turn that had said nothing.
                            raise _no_answer("the endpoint answered with no text "
                                             "and no tool call")
                        # The answer arrives as one object, but both interfaces
                        # render from the stream: they flush what they buffered when
                        # "done" comes, and they buffer nothing here. So the text is
                        # handed over the same way a streamed answer arrives, or the
                        # user watches a silent turn.
                        if callback and response:
                            callback("stream_delta", {"text": response})
                        parsed = ParsedResponse(
                            text=response,
                            commands=[ParsedCommand(str(c.get("tool", "")),
                                                    c.get("args") or {}) for c in calls],
                            has_commands=bool(calls))
                    else:
                        response = await self._stream_response(provider, messages, callback, model)
                        parsed = self.parser.parse(response)
                except Exception as e:
                    error_msg = f"Error calling provider: {e}"
                    if callback:
                        callback("error", {"message": error_msg})
                    return error_msg

                self.economy.request_count += 1

                if parsed.repaired:
                    # Say it out loud: a call fixed in silence teaches the model
                    # nothing and the user nothing.
                    if callback:
                        callback("tool_repaired", {"notes": parsed.repaired})

                # A call the parser could not read is reported every single time
                # it happens — to the user, and to the model in the transcript.
                # It used to be said once, and only when nothing else ran, which
                # is how "call A plus a cut-off call B" came back as "both notes
                # are saved".
                broken_note = ""
                if parsed.dropped:
                    broken_streak += 1
                    broken_note = _RESEND_CALL.format(note="; ".join(parsed.dropped))
                    if callback:
                        callback("tool_dropped", {"notes": parsed.dropped})
                else:
                    broken_streak = 0

                if not parsed.has_commands:
                    if broken_note:
                        # The model meant to act and its payload died on the way
                        # out. Ending the turn on "I will create the file now" is
                        # exactly what the user reads as being ignored.
                        session.add_assistant_message(response)
                        session.add_tool_result(broken_note)
                        if broken_streak >= BROKEN_CALL_CHANCES:
                            # Re-asked once and it broke again: this is not an
                            # answer, and handing it to `done` would sell a cut-off
                            # payload to the user as the final word.
                            gave_up = _gave_up_on_calls()
                            if callback:
                                callback("error", {"message": gave_up})
                            return gave_up
                        continue
                    session.add_assistant_message(response)
                    if not nudged and _PROMISE_TO_ACT.search(response or ""):
                        # "Now I will read the file" is a half-finished turn.
                        nudged = True
                        nudge_pending = True
                        if callback:
                            callback("nudged", {})
                        continue
                    self.economy.store_cache(prompt_str, model, response)
                    if callback:
                        # The text travels with the event: a plugin listening for
                        # the end of an answer should not have to reach into the
                        # session to find out what was said.
                        callback("done", {"text": response})
                    return response

                # Every call in this row owes its result from here on, including
                # a run that ends by cancellation between one call and the next.
                # What is still owed is read back off the transcript itself, so no
                # branch of the loop below can forget to account for itself.
                session.add_assistant_message(response, tool_calls=[
                    {"tool": cmd.tool, "args": cmd.args} for cmd in parsed.commands
                ])
                call_row = (len(session.messages), [cmd.tool for cmd in parsed.commands])

                # Execute tool calls one at a time; feed each result back.
                for cmd in parsed.commands:
                    asked = cmd.tool
                    tool = self.tools.get(asked)

                    if tool is not None:
                        # Aliases ("read_directory") are accepted but the UI and
                        # the history speak the canonical name.
                        canonical = self.tools.canonical_name(asked)
                        if canonical != asked:
                            cmd.tool = canonical
                            tool = self.tools.get(canonical)
                            if callback:
                                callback("tool_renamed", {"from": asked, "to": canonical})

                    if tool is None:
                        # Models mistype names ("reed" for "read"); a near
                        # exact match is safe to run, a guess is not.
                        near = self.tools.resolve(cmd.tool)
                        if near is not None:
                            if callback:
                                callback("tool_renamed", {"from": cmd.tool, "to": near})
                            cmd.tool = near
                            tool = self.tools.get(near)

                    if tool is None:
                        names = ", ".join(self.tools.list_names())
                        session.add_tool_result(
                            f"[tool result] Unknown tool '{cmd.tool}'. Available: {names}"
                        )
                        if callback:
                            callback("tool_unknown", {"tool": cmd.tool})
                        continue

                    # Loosely parsed calls (tag style, renamed args) are fitted
                    # to the tool's real parameters before we run them.
                    args = tool.coerce_args(cmd.args)
                    missing = tool.missing_args(args)
                    if missing:
                        session.add_tool_result(
                            f"[tool result] {cmd.tool} needs {', '.join(missing)}; "
                            f"its parameters are: {', '.join(tool.params())}"
                        )
                        if callback:
                            callback("tool_error", {
                                "tool": cmd.tool,
                                "message": f"missing argument(s): {', '.join(missing)}",
                            })
                        continue

                    # What the model wants is not what the user allowed: tools
                    # that touch the machine need a grant first.
                    if not self.permissions.allows(tool):
                        message = self.permissions.refusal(tool)
                        session.add_tool_result(f"[tool result] {message}")
                        self.permissions.denied_this_run.add(cmd.tool)
                        if callback:
                            callback("tool_denied", {
                                "tool": cmd.tool, "args": args, "message": message,
                            })
                        continue

                    # The tool may behave differently for a granted run (see
                    # BaseTool.granted); the gate above already refused otherwise.
                    tool.granted = self.permissions.mode == "auto" or                         cmd.tool in self.permissions.granted
                    if callback:
                        callback("tool_start", {"tool": cmd.tool, "args": args})

                    try:
                        # Tools are sync (subprocess, MCP, file IO); running them
                        # in a worker thread keeps the prompt and stream alive.
                        raw = await asyncio.to_thread(tool.execute, **args)
                    except Exception as e:
                        raw = ToolResult(output=f"ERROR: {e}", error=True)

                    # All of the bookkeeping is inside one guard, and the result is
                    # fitted to the ToolResult shape first: an extension that
                    # answers with a bare string has run its call, and reading
                    # `.output` off it here used to raise out of run() *after* the
                    # assistant row carrying the call was written — a transcript
                    # ending on a call with no result, replayed by every later turn.
                    try:
                        result = _as_tool_result(raw)
                        output = result.output if result.output.strip() else "(empty output)"
                    except Exception as e:
                        result = ToolResult(
                            output=f"ERROR: the tool answered in a shape BeeCode "
                                   f"could not read: {type(e).__name__}: {e}",
                            error=True)
                        output = result.output

                    result_text = (
                        f"[tool result] tool={cmd.tool} error={result.error}\n{output}"
                    )
                    if parsed.repaired:
                        result_text += (
                            "\n[format note] your call was incomplete — BeeCode "
                            + " and ".join(parsed.repaired)
                            + ". Write the whole block in one go: a line of "
                              "```json, then "
                              '{"tool": "name", "args": {"param": "value"}}, then '
                              "the closing ```."
                        )
                    session.add_tool_result(result_text)

                    if callback:
                        callback("tool_end", {
                            "tool": cmd.tool,
                            "args": args,
                            "output": output,
                            "error": result.error,
                        })

                if broken_note:
                    # Some calls ran and one did not: the model still has to hear
                    # which one it lost, or it answers next turn as if all of them
                    # went through.
                    session.add_tool_result(broken_note)

            # Max turns reached without a final answer — make it visible.
            if callback:
                callback("error", {"message": "Max turns reached without a final answer"})
            return "Max turns reached"
        except asyncio.CancelledError:
            # Answer every call the last assistant row still owes, not just the one
            # in flight: a cancel can land between two calls of one turn, and one
            # unanswered call is enough for a native-tools provider to reject the
            # whole replayed history.
            if call_row is not None:
                row, names = call_row
                answered = 0
                for message in session.messages[row:]:
                    if message.role != "tool":
                        break
                    answered += 1
                for name in names[min(answered, len(names)):]:
                    session.add_tool_result(
                        f"[tool result] tool={name} error=True\n{_ABANDONED}")
            raise
        finally:
            self.is_busy = False
            self.stop_requested = False

    def run_sync(self, user_input: str, session: Session = None, callback=None) -> str:
        return asyncio.run(self.run(user_input, session, callback))
