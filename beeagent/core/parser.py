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


_FENCE = "```"
_FENCE_JSON = _FENCE + "json"


def _json_spans(text: str):
    """Every balanced {...} span, with string escapes honoured.

    A regex cannot do this job: a tool payload nests braces, so a non-greedy
    brace match stops at the first inner one, and the closing fence it leaned on
    to reach the real end is something models often leave out — a several-KB
    HTML file handed to `write` ended exactly like that and was printed on the
    terminal instead of executed. Counting depth while skipping quoted text
    reads both shapes.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
        elif char == "}" and depth:
            depth -= 1
            if depth == 0:
                yield start, index + 1
                start = -1


def _fence_before(text: str, start: int) -> int:
    """Where the fence that introduced this object begins, or start itself."""
    head = text[:start].rstrip()
    for fence in (_FENCE_JSON, _FENCE):
        if head.endswith(fence):
            return len(head) - len(fence)
    return start


def _fence_after(text: str, end: int) -> int:
    """Where the fence closing this object ends, or end itself."""
    tail = text[end:]
    stripped = tail.lstrip()
    if stripped.startswith(_FENCE):
        return end + (len(tail) - len(stripped)) + len(_FENCE)
    return end


class CommandParser:

    def parse(self, response: str) -> ParsedResponse:
        commands = []
        cuts = []

        # 1. Any JSON object naming a tool: fenced, half-fenced, or bare.
        for start, end in _json_spans(response):
            try:
                data = loads_lenient(response[start:end])
            except (json.JSONDecodeError, ValueError):
                continue
            if not (isinstance(data, dict) and "tool" in data):
                continue
            args = data.get("args", {})
            commands.append(ParsedCommand(
                tool=str(data["tool"]),
                args=args if isinstance(args, dict) else {},
            ))
            cuts.append((_fence_before(response, start), _fence_after(response, end)))

        remaining = response
        for cut_start, cut_end in sorted(cuts, reverse=True):
            remaining = remaining[:cut_start] + remaining[cut_end:]

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
