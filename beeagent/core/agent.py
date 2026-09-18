import json
import asyncio
from beeagent.config.loader import load_config
from beeagent.config.schema import BeeConfig
from beeagent.providers.registry import ProviderRegistry
from beeagent.providers.g4f_provider import G4fProvider
from beeagent.tools.registry import ToolRegistry
from beeagent.tools.read import ReadTool
from beeagent.tools.write import WriteTool
from beeagent.tools.edit import EditTool
from beeagent.tools.bash import BashTool
from beeagent.tools.grep import GrepTool
from beeagent.tools.glob_tool import GlobTool
from beeagent.tools.web_search import WebSearchTool
from beeagent.tools.git import GitTool
from beeagent.tools.todo import TodoTool
from beeagent.tools.task import TaskTool
from beeagent.core.session import Session
from beeagent.core.context import ContextManager
from beeagent.core.economy import EconomyManager
from beeagent.core.parser import CommandParser


class Agent:
    def __init__(self, config: BeeConfig = None, workdir: str = "."):
        self.config = config or load_config(workdir)
        self.workdir = workdir
        self.parser = CommandParser()

        self.providers = ProviderRegistry()
        self.providers.register(G4fProvider())

        self.tools = ToolRegistry()
        for tool_cls in [ReadTool, WriteTool, EditTool, BashTool,
                         GrepTool, GlobTool, WebSearchTool, GitTool,
                         TodoTool, TaskTool]:
            self.tools.register(tool_cls())

        self.economy = EconomyManager(
            mode=self.config.mode,
            cache_dir=self.config.economy.cache_dir,
        )

        self.context = ContextManager(model=self.config.model)

    async def run(self, user_input: str, session: Session = None, callback=None) -> str:
        session = session or Session()
        session.add_user_message(user_input)

        provider = self.providers.fallback(self.config.provider)

        for turn in range(self.config.max_turns):
            tool_schemas = self.tools.to_schemas()
            messages = self.context.build_messages(session.to_dicts(), tool_schemas)

            prompt_str = json.dumps(messages)
            cached = self.economy.check_cache(prompt_str, self.config.model)
            if cached:
                if callback:
                    callback("economy_hit", {})
                return cached

            try:
                response = await provider.chat(messages, model=self.config.model)
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
                    callback("response", {"text": response})
                return response

            session.add_assistant_message(response, tool_calls=[
                {"tool": cmd.tool, "args": cmd.args} for cmd in parsed.commands
            ])

            results = []
            for cmd in parsed.commands:
                tool = self.tools.get(cmd.tool)

                if tool is None:
                    results.append(f"Error: Unknown tool '{cmd.tool}'")
                    if callback:
                        callback("tool_error", {"tool": cmd.tool, "message": "Unknown tool"})
                    continue

                if callback:
                    callback("tool_start", {"tool": cmd.tool, "args": cmd.args})
                else:
                    print(f"  [{cmd.tool}] ", end="", flush=True)

                try:
                    result = tool.execute(**cmd.args)
                except Exception as e:
                    result = type('ToolResult', (), {
                        'output': str(e),
                        'error': True,
                        'metadata': {}
                    })()

                if callback:
                    callback("tool_end", {
                        "tool": cmd.tool,
                        "args": cmd.args,
                        "output": result.output,
                        "error": result.error,
                    })
                else:
                    print("OK" if not result.error else "ERROR")

                results.append(result.output)
                session.add_tool_result(result.output)

        return "Max turns reached"

    def run_sync(self, user_input: str, session: Session = None, callback=None) -> str:
        return asyncio.run(self.run(user_input, session, callback))
