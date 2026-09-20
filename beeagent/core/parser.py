"""BeeCode command parser.

Extracts tool calls from model output. The XML-style tag is built dynamically
to avoid embedding the raw closing tag in this source file.
"""
import re
import json
from dataclasses import dataclass
from typing import Optional


@dataclass
class ParsedCommand:
    tool: str
    args: dict


@dataclass
class ParsedResponse:
    text: str
    commands: list[ParsedCommand]
    has_commands: bool


_TAG_NAME = "tool" + "_" + "call"
_TAG_OPEN = "<" + _TAG_NAME
_TAG_CLOSE = "</" + _TAG_NAME + ">"
_TAG_PATTERN = re.compile(
    _TAG_OPEN + r"\s*(\w+)\s*\n(.*?)" + _TAG_CLOSE,
    re.DOTALL,
)

# A backslash that cannot start a JSON escape — typical for a Windows path the
# model wrote as "C:\Users\proj" instead of "C:\\Users\\proj".
_STRAY_BACKSLASH = re.compile(r'\\(?!["\\/bfnrtu])')


def loads_lenient(payload: str):
    """json.loads that survives single-backslash Windows paths."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return json.loads(_STRAY_BACKSLASH.sub(r"\\\\", payload))


class CommandParser:

    def parse(self, response: str) -> ParsedResponse:
        commands = []
        remaining = response

        # 1. ```json fenced blocks (primary format).
        code_block_pattern = re.compile(r'```(?:json)?\s*\n?(\{.*?\})\s*\n?```', re.DOTALL)
        for match in code_block_pattern.finditer(response):
            try:
                data = loads_lenient(match.group(1))
                if isinstance(data, dict) and "tool" in data:
                    args = data.get("args", {})
                    if isinstance(args, dict):
                        commands.append(ParsedCommand(
                            tool=str(data["tool"]),
                            args=args,
                        ))
                        remaining = remaining.replace(match.group(0), "", 1)
            except (json.JSONDecodeError, KeyError):
                continue

        # 2. Bare JSON objects with "tool" key (e.g. inline in prose).
        plain_json_pattern = re.compile(r'\{"tool"\s*:\s*"(\w+)"\s*,\s*"args"\s*:\s*(\{.*?\})\s*\}')
        for match in plain_json_pattern.finditer(remaining):
            try:
                tool_name = match.group(1)
                args = loads_lenient(match.group(2))
                if isinstance(args, dict):
                    commands.append(ParsedCommand(
                        tool=tool_name,
                        args=args,
                    ))
                    remaining = remaining.replace(match.group(0), "", 1)
            except json.JSONDecodeError:
                continue

        # 3. <tool_call>TAG\nargs\n> tag fallback.
        for match in _TAG_PATTERN.finditer(remaining):
            tool_name = match.group(1)
            args_str = match.group(2).strip()
            args = self._parse_simple_args(args_str)
            commands.append(ParsedCommand(
                tool=tool_name,
                args=args,
            ))
            remaining = remaining.replace(match.group(0), "", 1)

        text_parts = [line.rstrip() for line in remaining.strip().split("\n") if line.strip()]
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
        close = "</" + _TAG_NAME + ">"
        lines = [
            "You can use these tools. Respond with EXACTLY one JSON code block:",
            "",
            '```json',
            '{"tool": "tool_name", "args": {"param": "value"}}',
            '```',
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
