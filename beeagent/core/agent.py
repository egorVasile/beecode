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


class Agent:
    def __init__(self, config: BeeConfig = None, workdir: str = "."):
        self.config = config or load_config(workdir)
        self.workdir = workdir

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

    def _parse_tool_calls(self, content: str) -> list[dict]:
        calls = []
        lines = content.strip().split("\n")
        for line in lines:
            line = line.strip()
            if line.startswith("```json"):
                line = line[7:]
            if line.startswith("```"):
                line = line[3:]
            if not line:
                continue
            try:
                data = json.loads(line)
                if isinstance(data, dict) and "tool" in data:
                    calls.append(data)
            except json.JSONDecodeError:
                continue
        return calls

    def _format_tools_for_prompt(self) -> str:
        lines = ["\nYou have access to these tools:"]
        for tool in self.tools.list_tools():
            lines.append(f"\n<tool name=\"{tool.name}\">")
            lines.append(f"  {tool.description}")
            lines.append(f"  Parameters: {json.dumps(tool.parameters)}")
            if tool.is_safe():
                lines.append(f"  [SAFE - auto-approve]")
            lines.append("</tool>")

        lines.append("\n\nTo use a tool, respond with a JSON line:")
        lines.append('{"tool": "tool_name", "args": {"param": "value"}}')
        lines.append("You can call multiple tools in one response (one JSON per line).")
        lines.append("When done, respond with regular text (no JSON).")
        return "\n".join(lines)

    async def run(self, user_input: str, session: Session = None) -> str:
        session = session or Session()
        session.add_user_message(user_input)

        provider = self.providers.fallback(self.config.provider)

        for turn in range(self.config.max_turns):
            tool_schemas = self.tools.to_schemas()
            messages = self.context.build_messages(session.to_dicts(), tool_schemas)
            messages[0]["content"] += self._format_tools_for_prompt()

            prompt_str = json.dumps(messages)
            cached = self.economy.check_cache(prompt_str, self.config.model)
            if cached:
                print(f"[economy: cache hit]")
                return cached

            try:
                response = await provider.chat(messages, model=self.config.model)
            except Exception as e:
                return f"Error calling provider: {e}"

            self.economy.request_count += 1

            tool_calls = self._parse_tool_calls(response)

            if not tool_calls:
                session.add_assistant_message(response)
                self.economy.store_cache(prompt_str, self.config.model, response)
                return response

            session.add_assistant_message(response, tool_calls=tool_calls)

            results = []
            for call in tool_calls:
                tool_name = call["tool"]
                tool_args = call.get("args", {})
                tool = self.tools.get(tool_name)

                if tool is None:
                    results.append(f"Error: Unknown tool '{tool_name}'")
                    continue

                task_type = tool_name
                model = self.economy.select_model(task_type, self.config.model)

                print(f"  [{tool_name}] ", end="", flush=True)
                result = tool.execute(**tool_args)
                print("OK" if not result.error else "ERROR")

                results.append(result.output)
                session.add_tool_result(result.output)

        return "Max turns reached"

    def run_sync(self, user_input: str, session: Session = None) -> str:
        return asyncio.run(self.run(user_input, session))
