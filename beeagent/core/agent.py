"""BeeAgent core agent with realtime streaming and tool feedback loop.

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
from beeagent.core.parser import CommandParser


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

        self.tools = ToolRegistry()
        for tool_cls in [ReadTool, WriteTool, EditTool, BashTool,
                         GrepTool, GlobTool, ListDirectoryTool, WebSearchTool,
                         GitTool, TodoTool]:
            self.tools.register(tool_cls())

        self.economy = EconomyManager(
            mode=self.config.mode,
            cache_dir=self.config.economy.cache_dir,
        )

        self.context = ContextManager(model=self.config.model)
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

    def reload_extensions(self) -> list[str]:
        """Re-scan installed skills, plugin packs and MCP servers after a change."""
        self.plugins.reset()
        try:
            self.plugins.load_all()
        except Exception as e:
            self.plugins.load_errors.append(f"plugin loader failed: {e}")
        self.context.skills_section = self.plugins.skills_prompt_section()
        return self.plugins.load_errors

    async def _stream_response(self, provider, messages, callback) -> str:
        """Stream the model response with retry.

        Emits "reasoning_delta" for thinking tokens and "stream_delta" for
        answer tokens. Raises only when all attempts fail.
        """
        last_error = None
        for attempt in range(3):
            if callback and attempt > 0:
                callback("retry", {"attempt": attempt + 1})

            if hasattr(provider, "chat_stream"):
                emitted = False
                try:
                    answer, reasoning = [], []
                    async for kind, text in provider.chat_stream(messages, model=self.config.model):
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
                    if content.strip():
                        return content
                    last_error = "empty response"
                except Exception as e:
                    last_error = e  # fall through to retry
                if emitted and callback:
                    # Partial text already reached the UI; have it drop that
                    # fragment so the fallback answer prints cleanly.
                    callback("stream_reset", {})

            try:
                text = await provider.chat(messages, model=self.config.model)
                if text and text.strip():
                    # Non-stream fallback: the UI never saw this text, so
                    # emit it as one delta or the answer is silently lost.
                    if callback:
                        callback("stream_delta", {"text": text})
                    return text
                last_error = "empty response"
            except Exception as e:
                last_error = e

            await asyncio.sleep(1.5 * (attempt + 1))  # backoff

        raise RuntimeError(L(
            f"the provider returned an empty answer after all attempts "
            f"(last error: {last_error}) — try again or switch model (/models)",
            f"провайдер вернул пустой ответ после всех попыток "
            f"(последняя ошибка: {last_error}) — попробуй ещё раз или смени модель (/models)",
        ))

    async def run(self, user_input: str, session: Session = None, callback=None) -> str:
        session = session or Session()
        session.add_user_message(user_input)

        provider = self.providers.select(self.config.provider)
        self.is_busy = True
        trim_reported = False

        try:
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
                messages = self.context.build_messages(session.to_dicts(), tool_schemas)

                prompt_str = json.dumps(messages)
                if self.context.trimmed and not trim_reported:
                    # Tell the user why the model may look forgetful this turn.
                    trim_reported = True
                    if callback:
                        callback("context_trimmed", {"dropped": self.context.trimmed})
                cached = self.economy.check_cache(prompt_str, self.config.model)
                if cached:
                    if callback:
                        callback("economy_hit", {})
                        callback("response", {"text": cached})
                    return cached

                try:
                    response = await self._stream_response(provider, messages, callback)
                except Exception as e:
                    error_msg = f"Error calling provider: {e}"
                    if callback:
                        callback("error", {"message": error_msg})
                    return error_msg

                self.economy.request_count += 1

                parsed = self.parser.parse(response)

                if not parsed.has_commands:
                    session.add_assistant_message(response)
                    self.economy.store_cache(prompt_str, self.config.model, response)
                    if callback:
                        callback("done", {})
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
