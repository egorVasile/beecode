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
from beeagent.tools.list_dir import ListDirectoryTool
from beeagent.tools.web_search import WebSearchTool
from beeagent.tools.git import GitTool
from beeagent.tools.todo import TodoTool
from beeagent.core.queue import PendingQueue
from beeagent.core.session import Session
from beeagent.core.context import ContextManager
from beeagent.core.economy import EconomyManager
from beeagent.core.permissions import Permissions
from beeagent.core.parser import CommandParser


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
_RESEND_CALL = ("[BeeCode] Your tool call arrived broken, so nothing was run: {note}. "
                "Send it again in one ```json block, with every value on one line — "
                "write a newline inside a string as \\n, a backslash as \\\\ and a "
                "quote as \\\". If the content is long, write the first part with "
                "`write` and add the rest with `edit`.")


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
        for endpoint in ENDPOINTS:
            key = key_for(endpoint, self.config.api_keys)
            if not key:
                continue
            self.providers.register(OpenAICompatProvider(
                base_url=endpoint.url, api_key=key,
                model=endpoint.models[0] if endpoint.models else "gpt-4",
                name=endpoint.name, models=endpoint.models,
            ))
            self.ready_presets.append(endpoint.name)

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
                         GitTool, TodoTool]:
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
        self.parser = CommandParser()

        # Skills, plugin tool packs, and MCP servers installed from the catalog.
        from beeagent.plugins.loader import PluginLoader
        self.plugins = PluginLoader(self)
        try:
            self.plugins.load_all()
        except Exception:
            # A broken extension must never take the agent down.
            self.plugins.load_errors.append("plugin loader failed")

        # Messages typed while the agent is busy; delivered with the next
        # model call so nothing the user says is lost.
        self.pending = PendingQueue()
        self.is_busy = False

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

    def _is_prompt_echo(self, content: str, messages: list[dict]) -> bool:
        text = (content or "").strip()
        if not text:
            return False
        if any(marker in text for marker in self.ECHO_MARKERS):
            return True
        if len(text) < 60:
            return False
        sent = " ".join(str(m.get("content") or "") for m in messages)
        return " ".join(text.split()).lower() in " ".join(sent.split()).lower()

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

        raise RuntimeError(L(
            f"the provider returned an empty answer after all attempts "
            f"(last error: {last_error}) — try again or switch model (/models)",
            f"провайдер вернул пустой ответ после всех попыток "
            f"(последняя ошибка: {last_error}) — попробуй ещё раз или смени модель (/models)",
        ))

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

    async def run(self, user_input: str, session: Session = None, callback=None) -> str:
        session = session or Session()
        session.add_user_message(user_input)

        # is_busy is set before anything can raise: an unconfigured provider
        # used to leave it True forever, and then the REPL parked every later
        # message in agent.pending and never ran a single one.
        self.is_busy = True
        self.permissions.denied_this_run.clear()

        try:
            provider = self.providers.select(self.config.provider)
            model = self._model_for(provider, callback)
            trim_reported = False
            nudged = False
            nudge_pending = False
            rescued = False

            for turn in range(self.config.max_turns):
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
                messages = self.context.build_messages(session.to_dicts(), tool_schemas)
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
                    response = await self._stream_response(provider, messages, callback, model)
                except Exception as e:
                    error_msg = f"Error calling provider: {e}"
                    if callback:
                        callback("error", {"message": error_msg})
                    return error_msg

                self.economy.request_count += 1

                parsed = self.parser.parse(response)

                if parsed.repaired:
                    # Say it out loud: a call fixed in silence teaches the model
                    # nothing and the user nothing.
                    if callback:
                        callback("tool_repaired", {"notes": parsed.repaired})

                if not parsed.has_commands:
                    if parsed.dropped and not rescued:
                        # The model meant to act and its payload died on the way
                        # out. Ending the turn on "I will create the file now" is
                        # exactly what the user reads as being ignored.
                        rescued = True
                        session.add_assistant_message(response)
                        session.add_tool_result(
                            _RESEND_CALL.format(note="; ".join(parsed.dropped)))
                        if callback:
                            callback("tool_dropped", {"notes": parsed.dropped})
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

                session.add_assistant_message(response, tool_calls=[
                    {"tool": cmd.tool, "args": cmd.args} for cmd in parsed.commands
                ])

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

                    if callback:
                        callback("tool_start", {"tool": cmd.tool, "args": args})

                    try:
                        # Tools are sync (subprocess, MCP, file IO); running them
                        # in a worker thread keeps the prompt and stream alive.
                        result = await asyncio.to_thread(tool.execute, **args)
                    except Exception as e:
                        result = ToolResult(output=f"ERROR: {e}", error=True)

                    output = result.output if result.output.strip() else "(empty output)"
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

            # Max turns reached without a final answer — make it visible.
            if callback:
                callback("error", {"message": "Max turns reached without a final answer"})
            return "Max turns reached"
        finally:
            self.is_busy = False

    def run_sync(self, user_input: str, session: Session = None, callback=None) -> str:
        return asyncio.run(self.run(user_input, session, callback))
