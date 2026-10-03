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
from beeagent.core import streaming
from beeagent.config.loader import load_config
from beeagent.config.schema import BeeConfig
from beeagent.providers.registry import ProviderRegistry
from beeagent.providers.g4f_provider import G4fProvider
from beeagent.providers.ollama import OllamaProvider
from beeagent.providers.openai_compat import OpenAICompatProvider
from beeagent.tools.registry import ToolRegistry
from beeagent.core import executor
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
from beeagent.tools.think import ThinkTool
from beeagent.tools.ask import AskTool
from beeagent.tools.table import TableTool
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
           "Either call a tool now (use `think` to plan, `ask` to ask the user, or "
           "any other tool) or answer without promising to act.]")

# Planning is a step, not the work: two think-only turns in a row means the
# model is re-planning instead of acting (seen live: thirty plans, zero tool
# calls, thousands of tokens). Warn once, then stop — max_turns would only
# bill the same loop fifty times.
THINK_LOOP = ("[SYSTEM: two plans in a row and no action. Planning is over: "
              "your next message MUST contain a real working tool call "
              "(list_directory, read, write, edit, bash, diagnostics, ...) — "
              "not another think, not prose about the plan.]")
THINK_LOOP_STOP = ("Stopping: the model planned four times in a row without a "
                   "single working tool call, so this run is going nowhere. "
                   "Ask again with a smaller first step.")

# A model that answers "I have no file access" is not finished either — the
# tools are in this conversation, named, with schemas. Seen live 2026-09-30:
# a free-routed model told the user to attach an archive of the folder instead
# of reading it. Same one-nudge rule as a promised step: a second refusal
# stands, because insisting twice is how a loop starts.
_NO_ACCESS = re.compile(
    r"(нет доступа|не могу получить доступ|не имею доступа|открой[те]? доступ|"
    r"прикрепи(те)? (архив|файл|папку)|загрузи(те)? (файл|архив|папку)|"
    r"no access|don't have access|do not have access|can't access|cannot access|"
    r"i (am |’m )?(just|only)? ?(a |an )?(text|language|chat|ai|language model)|"
    r"as an ai\b.{0,80}?(can't|cannot|unable))",
    re.I,
)
YOU_HAVE_TOOLS = ("[SYSTEM: you DO have file access — read, grep, glob and "
                  "list_directory are in this conversation with their schemas, "
                  "and the working directory is real. Either call one now or "
                  "answer without claiming you cannot.]")

# A greeting instead of an answer: the user asked something real and the
# model replied "Ready, what can I do for you" — or learned a new shape of
# the same dodge ("BeeCode here — what's the task?"). Nudged once, and only
# when the question was substantive — a bare "ку" deserves its "Ку! Чем
# помочь?". Matched anywhere in a short answer: greetings hide mid-sentence.
_GREETING = re.compile(
    r"^(hi|hello|hey|ready|greetings|yo|ку|привет|здравствуй|добрый день|"
    r"hello!|hi!)[.!…\s]*(what|how can i|чем помочь|чем могу|что нужно|"
    r"что (я могу|могу я)|ready to help|here to help|assist you)?.*$"
    r"|what('s| is) the task\b.{0,60}$"
    r"|how can i help( you)?\b"
    r"|чем помочь\b.{0,40}$",
    re.I,
)
ANSWER_IT = ("[SYSTEM: that was a greeting, but the user asked a question. "
             "Answer it or call a tool — do not greet back.]")

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


from beeagent.core.streaming import _no_answer as _no_answer
from beeagent.core.streaming import _reason as _reason

# Re-exported for the tests that learned these names here; the streaming
# layer in core/streaming.py is where they live now.
__all__ = ["Agent", "_no_answer", "_reason"]


def _carries_a_call(parser, text: str) -> bool:
    """Whether this reply is a step of the loop rather than an answer.

    A cached reply that names a tool — even one whose payload arrived cut off —
    must not be served from the cache: it would print and run nothing.
    """
    parsed = parser.parse(text)
    return parsed.has_commands or bool(parsed.dropped)


class Agent:
    def __init__(self, config: BeeConfig = None, workdir: str = "."):
        self.config = config or load_config(workdir)
        self.workdir = workdir

        self.providers = ProviderRegistry()
        self.providers.register(G4fProvider())
        self.provider_errors: list[str] = []
        # Providers the user configured in beeagent.json; without this they are
        # advertised by /providers but /provider <name> silently fell back to g4f.
        from beeagent.core import provider_setup

        for custom in self.config.custom_providers:
            try:
                provider_setup.register_custom_endpoint(self, self.config, custom)
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
            if key or endpoint.keyless:
                self.attach_preset(endpoint.name, key)

        # A pool the operator runs. Registered as soon as its address is known —
        # with no seat yet the provider answers "run /pool enroll" instead of
        # "unknown provider", and neither the address nor the seat is a key.
        from beeagent.providers.pool import PoolProvider
        pool = PoolProvider(
            url=self.config.pool_url, token=self.config.pool_token,
            idle_timeout=max(10, int(self.config.stream_idle_timeout or 90)))
        # Auto re-enroll rewrites the live token; persist it so the next
        # launch does not enroll all over again.
        pool.on_token = self._save_pool_token
        self.providers.register(pool)

        self.tools = ToolRegistry()
        for tool_cls in [ReadTool, WriteTool, EditTool, BashTool,
                         GrepTool, GlobTool, ListDirectoryTool, WebSearchTool,
                         WebFetchTool, GitTool, TodoTool, DiagramTool,
                         DiagnosticsTool, PatchTool, MoveTool, RemoveTool,
                         ThinkTool, AskTool, TableTool]:
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
        self.sync_context()
        # A measured window belongs to the endpoint that measured it, and
        # ContextManager asks for a window by model name alone — so the name the
        # reads should resolve against is set here and refreshed on every run.
        windows.note_provider(self.config.provider or "")
        self.parser = CommandParser()

        # Skills, plugin tool packs, and MCP servers installed from the catalog.
        from beeagent.plugins.loader import PluginLoader
        self.plugins = PluginLoader(self, gate_project=True)
        try:
            self.plugins.load_all()
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            # A broken extension must never take the agent down.
            self.plugins.load_errors.append("plugin loader failed")

        # Messages typed while the agent is busy; delivered with the next
        # model call so nothing the user says is lost.
        self.pending = PendingQueue()
        # The tool in flight, named by core/executor.py; the cancel handler
        # in run() reads it to interrupt (see running_tool above).
        self.running_tool = None
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
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception as e:
            self.plugins.load_errors.append(f"plugin loader failed: {e}")
        self.context.skills_section = self.plugins.skills_prompt_section()
        return self.plugins.load_errors

    # --- streaming: the logic lives in core/streaming.py. These are the
    # stable doors the loop, the tests and the UI knock on — behaviour is
    # identical, only the address changed.
    ECHO_MARKERS = streaming.ECHO_MARKERS
    ECHO_MIN_CHARS = streaming.ECHO_MIN_CHARS
    ECHO_SHARE = streaming.ECHO_SHARE

    def _is_prompt_echo(self, content: str, messages: list[dict]) -> bool:
        return streaming.is_prompt_echo(content, messages)

    async def _next_token(self, iterator, idle: int, callback, announce: bool):
        return await streaming.next_token(
            iterator, idle, callback, announce, heartbeat=HEARTBEAT_SECONDS)

    async def _stream_response(self, provider, messages, callback, model: str = "") -> str:
        return await streaming.stream_response(
            provider, messages, callback,
            model=model or self.config.model,
            idle=self.config.stream_idle_timeout,
            heartbeat=HEARTBEAT_SECONDS)

    # --- provider resolution: the logic lives in core/provider_setup.py.
    # These are the stable doors the loop, the UI and the tests knock on.
    def _save_pool_token(self, token: str) -> None:
        """Persist a refreshed seat token; failures stay live-only."""
        try:
            self.config.pool_token = token
            from beeagent.config.loader import save_config

            save_config(self.config, self.workdir)
        except Exception:
            pass

    def sync_context(self) -> None:
        from beeagent.core import provider_setup

        provider_setup.sync_context(self)

    def _model_for(self, provider, callback=None) -> str:
        from beeagent.core import provider_setup

        return provider_setup.model_for(self, provider, callback)

    @staticmethod
    def _g4f_installed() -> bool:
        from beeagent.core import provider_setup

        return provider_setup.g4f_installed()

    def _provider_or_pool(self, callback):
        from beeagent.core import provider_setup

        return provider_setup.provider_or_pool(self, callback)

    def preset_provider(self, name: str, key: str):
        from beeagent.core import provider_setup

        return provider_setup.preset_provider(self, name, key)

    def attach_preset(self, name: str, key: str) -> bool:
        from beeagent.core import provider_setup

        return provider_setup.attach_preset(self, name, key)

    async def _ask_route(self, error):
        from beeagent.core import provider_setup

        return await provider_setup.ask_route(self, error)

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
            think_streak = 0
            think_warned = False
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
                    messages = messages + [{"role": "user", "content": nudge_pending}]
                    nudge_pending = False

                prompt_str = json.dumps(messages)
                shed = list(getattr(self.context, "shed", []) or [])
                if (self.context.trimmed or shed) and not trim_reported:
                    # Tell the user why the model may look forgetful this turn.
                    # Shed header parts (skills, permissions) used to vanish
                    # with trimmed==0 and no event at all.
                    trim_reported = True
                    if callback:
                        callback("context_trimmed", {"dropped": self.context.trimmed,
                                                     "shed": shed})
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

                held_text = ""
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
                        # user watches a silent turn — but only when no calls ride
                        # along: text shown before its own tools run reads as
                        # "done" followed by more work. Held text goes out after
                        # the tools below finish.
                        if calls:
                            parsed = ParsedResponse(
                                text=response,
                                commands=[ParsedCommand(str(c.get("tool", "")),
                                                        c.get("args") or {}) for c in calls],
                                has_commands=True)
                            held_text = response
                        else:
                            # The gateway did not decode a call, but the text
                            # may still carry one: weak models answer the native
                            # tools array with ```json or <name>/<arguments>.
                            # Read the text exactly like the streamed path does
                            # instead of selling it as the turn's answer.
                            parsed = self.parser.parse(response)
                            if parsed.has_commands:
                                held_text = response
                        if callback and response and not held_text:
                            callback("stream_delta", {"text": response})
                    else:
                        response = await self._stream_response(provider, messages, callback, model)
                        parsed = self.parser.parse(response)
                except asyncio.CancelledError:
                    raise
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
                        nudge_pending = ACT_NOW
                        if callback:
                            callback("nudged", {})
                        continue
                    if not nudged and _NO_ACCESS.search(response or ""):
                        # "I have no file access" with the tools in context.
                        nudged = True
                        nudge_pending = YOU_HAVE_TOOLS
                        if callback:
                            callback("nudged", {})
                        continue
                    if not nudged and len(response or "") < 150 \
                            and _GREETING.search((response or "").strip()):
                        # A greeting answering a real question. The question is
                        # what the user actually asked this turn, not the whole
                        # transcript: a short follow-up earns its greeting.
                        question = ""
                        for message in reversed(session.messages):
                            if message.role == "user":
                                question = str(message.content or "")
                                break
                        if len(question) > 25:
                            nudged = True
                            nudge_pending = ANSWER_IT
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
                # The how lives in core/executor.py; the loop only owes the
                # assistant row above and the broken-note below.
                await executor.execute_commands(self, session, parsed, callback)

                if held_text and callback:
                    # The summary the model wrote ahead of its calls: now that
                    # they ran, it reads in order.
                    callback("stream_delta", {"text": held_text})
                    held_text = ""

                if parsed.commands and all(cmd.tool == "think"
                                           for cmd in parsed.commands):
                    think_streak += 1
                    if think_streak >= 4:
                        if callback:
                            callback("error", {"message": THINK_LOOP_STOP})
                        return THINK_LOOP_STOP
                    if think_streak >= 2 and not think_warned:
                        think_warned = True
                        nudge_pending = THINK_LOOP
                        if callback:
                            callback("nudged", {})
                else:
                    think_streak = 0

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
            # Kill the tool that was in flight: a bash command left orphaned
            # can write files indefinitely after the agent loop has stopped.
            # The executor names it on `self.running_tool` (an instance
            # attribute, not a local: the handler below cannot see locals).
            running = getattr(self, "running_tool", None)
            if running is not None:
                interrupt = getattr(running, "interrupt", None)
                if callable(interrupt):
                    interrupt()
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
