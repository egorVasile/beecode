import re
import json
from dataclasses import dataclass
from typing import Optional

@dataclass
class ParsedCommand:
    tool: str
    args: dict
    raw: str

@dataclass
class ParsedResponse:
    text: str
    commands: list[ParsedCommand]
    has_commands: bool

class CommandParser:

    def parse(self, response: str) -> ParsedResponse:
        commands = []
        remaining = response

        code_block_pattern = re.compile(r'```json\s*\n?\s*(\{.*?\})\s*\n?```', re.DOTALL)
        for match in code_block_pattern.finditer(response):
            try:
                json_str = match.group(1)
                data = json.loads(json_str)
                if isinstance(data, dict) and "tool" in data:
                    tool_name = data["tool"]
                    args = data.get("args", {})
                    commands.append(ParsedCommand(
                        tool=tool_name,
                        args=args,
                        raw=match.group(0),
                    ))
                    remaining = remaining.replace(match.group(0), "", 1)
            except (json.JSONDecodeError, KeyError):
                continue

        plain_json_pattern = re.compile(r'\{"tool"\s*:\s*"(\w+)"\s*,\s*"args"\s*:\s*(\{[^}]*\})\s*\}')
        for match in plain_json_pattern.finditer(remaining):
            try:
                tool_name = match.group(1)
                args_str = match.group(2)
                args = json.loads(args_str)
                commands.append(ParsedCommand(
                    tool=tool_name,
                    args=args,
                    raw=match.group(0),
                ))
                remaining = remaining.replace(match.group(0), "", 1)
            except json.JSONDecodeError:
                continue

        tool_call_pattern = re.compile(r'<tool_call>\s*(\w+)\s*\n(.*?)\s*</tool_call>', re.DOTALL)
        for match in tool_call_pattern.finditer(remaining):
            tool_name = match.group(1)
            args_str = match.group(2).strip()
            args = self._parse_simple_args(args_str)
            commands.append(ParsedCommand(
                tool=tool_name,
                args=args,
                raw=match.group(0),
            ))
            remaining = remaining.replace(match.group(0), "", 1)

        text_parts = [line.strip() for line in remaining.strip().split("\n") if line.strip()]
        clean_text = "\n".join(text_parts)

        return ParsedResponse(
            text=clean_text,
            commands=commands,
            has_commands=len(commands) > 0,
        )

    def _parse_simple_args(self, args_str: str) -> dict:
        args = {}
        args_str = args_str.strip()

        key_value_pattern = re.compile(r'(\w+)\s*[:=]\s*"([^"]*)"')
        for match in key_value_pattern.finditer(args_str):
            args[match.group(1)] = match.group(2)

        if not args and args_str:
            parts = args_str.split(",")
            if len(parts) == 1:
                args["input"] = parts[0].strip().strip('"')
            else:
                for i, part in enumerate(parts):
                    args[f"arg{i}"] = part.strip().strip('"')

        return args

    def format_tool_prompt(self, tools: list[dict]) -> str:
        lines = [
            "\n## Available Tools",
            "",
            "You have access to the following tools. To use a tool, respond with EXACTLY this format:",
            "",
            '```json',
            '{"tool": "tool_name", "args": {"param1": "value1", "param2": "value2"}}',
            '```',
            "",
            "You can call multiple tools in one response (one JSON block per tool).",
            "When done, respond with regular text (no JSON blocks).",
            "",
        ]

        for tool in tools:
            params_desc = []
            props = tool.get("parameters", {}).get("properties", {})
            required = tool.get("parameters", {}).get("required", [])

            for pname, pinfo in props.items():
                req = " (required)" if pname in required else ""
                params_desc.append(f"    - {pname}: {pinfo.get('description', '')}{req}")

            lines.append(f"### {tool['name']}")
            lines.append(f"  {tool['description']}")
            if params_desc:
                lines.append("  Parameters:")
                lines.extend(params_desc)
            lines.append("")

        lines.append("## Rules")
        lines.append("- Always use JSON format for tool calls")
        lines.append("- Include ALL required parameters")
        lines.append("- When the task is complete, respond with text only (no JSON)")
        lines.append("- Think step by step before acting")
        lines.append("")

        return "\n".join(lines)
