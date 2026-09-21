"""BeeCode command parser.

Extracts tool calls from model output. The XML-style tag is built dynamically
to avoid embedding the raw closing tag in this source file.

Two rules hold this module together:

* A call is repaired in its *structure* only. Brackets, commas, quotes and
  escapes can be inferred; a value can never be invented, because a guessed
  file body would be written to disk and reported as a success.
* A block that names a tool never survives as prose. Either it runs, or it is
  taken out of what the user reads and the model is told what arrived broken —
  watching JSON scroll past is not a diagnosis.
"""
import re
import json
from dataclasses import dataclass, field


@dataclass
class ParsedCommand:
    tool: str
    args: dict


@dataclass
class ParsedResponse:
    text: str
    commands: list[ParsedCommand]
    has_commands: bool
    # What had to be fixed to read the call at all, so the model can be told.
    repaired: list[str] = field(default_factory=list)
    # Calls recognised but not run: cut off mid-value, or unreadable.
    dropped: list[str] = field(default_factory=list)


_TAG_NAME = "tool" + "_" + "call"
_TAG_OPEN = "<" + _TAG_NAME
_TAG_CLOSE = "</" + _TAG_NAME + ">"
_TAG_PATTERN = re.compile(_TAG_OPEN + r"\s*(\w+)[\s\n]*(.*?)" + _TAG_CLOSE, re.DOTALL)

# A backslash that cannot start a JSON escape — typical for a Windows path the
# model wrote as "C:\Users\proj" instead of "C:\\Users\\proj".
_STRAY_BACKSLASH = re.compile(r'\\(?!["\\/bfnrtu])')

_FENCE = "```"
_FENCE_JSON = _FENCE + "json"

_TRAILING_COMMA = re.compile(r",\s*$")
_COMMA_BEFORE_CLOSER = re.compile(r",(\s*[}\]])")
_BARE_WORD = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*|-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_COLON_NEXT = re.compile(r"\s*:")
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?$")

# The keys a model reaches for when it means "call this tool".
_TOOL_KEYS = ("tool", "tool_name", "name", "function")
_ARGS_KEYS = ("args", "arguments", "parameters", "params")
_TOOL_MENTION = re.compile(r'"(?:%s)"\s*:' % "|".join(_TOOL_KEYS))
_TOOL_NAME = re.compile(r'"(?:%s)"\s*:\s*"([^"\n]{1,40})"' % "|".join(_TOOL_KEYS))
_OPEN_KEY = re.compile(r'"([^"\n]{1,40})"\s*:\s*"')

_LITERALS = {"True": "true", "False": "false", "None": "null", "Null": "null",
             "NaN": "null", "undefined": "null", "nan": "null"}


def loads_lenient(payload: str):
    """json.loads that survives single-backslash Windows paths."""
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return json.loads(_STRAY_BACKSLASH.sub(r"\\\\", payload))


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


def _open_object(text: str):
    """The first brace that never closes: (start, closers, cut_inside_string).

    Returns None when every object in the text is balanced.
    """
    stack: list[str] = []
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
        elif char in "{[":
            if not stack:
                start = index
            stack.append("}" if char == "{" else "]")
        elif char in "}]" and stack:
            stack.pop()
            if not stack:
                start = -1
    if not stack:
        return None
    return start, "".join(reversed(stack)), in_string


def _normalize_json(payload: str) -> tuple[str, list[str]]:
    """Rewrite the shapes models actually emit into JSON that parses.

    Structural fixes only: a raw newline inside a quoted value — the most
    common way a file write dies, because the model pressed Enter instead of
    writing \\n — a key without quotes, single quotes for a string, a comma
    missing between two members, Python's True/False/None, and a comma left
    hanging before a closer.
    """
    out: list[str] = []
    notes: list[str] = []
    stack: list[str] = []
    in_string = False
    quote = '"'
    escaped = False
    value_complete = False      # the previous token finished a value
    index = 0
    size = len(payload)

    def fix(text: str) -> None:
        if text not in notes:
            notes.append(text)

    def separate() -> None:
        if value_complete:
            out.append(", ")
            fix("added a missing comma")

    while index < size:
        char = payload[index]

        if in_string:
            if escaped:
                out.append(char)
                escaped = False
            elif char == "\\":
                out.append(char)
                escaped = True
            elif char == quote:
                out.append('"')
                in_string = False
                value_complete = True
            elif quote != '"' and char == '"':
                out.append('\\"')
            elif char == "\n":
                out.append("\\n")
                fix("escaped a raw newline inside a string value")
            elif char == "\r":
                pass
            elif char == "\t":
                out.append("\\t")
            else:
                out.append(char)
            index += 1
            continue

        if char in '"“”':
            separate()
            quote = '"' if char == '"' else char
            in_string = True
            out.append('"')
            if char != '"':
                fix("replaced curly quotes")
            value_complete = False
            index += 1
            continue

        if char in "'‘’":
            separate()
            quote = "'" if char == "'" else char
            in_string = True
            out.append('"')
            fix("read a single-quoted string as JSON")
            value_complete = False
            index += 1
            continue

        if char in "{[":
            separate()
            stack.append(char)
            out.append(char)
            value_complete = False
            index += 1
            continue

        if char in "}]":
            text = "".join(out).rstrip()
            if text.endswith(","):
                text = text[:-1].rstrip()
                fix("dropped a trailing comma")
            out = [text, char]
            if stack:
                stack.pop()
            value_complete = True
            index += 1
            continue

        if char in ":,":
            out.append(char)
            value_complete = False
            index += 1
            continue

        if char.isspace():
            out.append(char)
            index += 1
            continue

        word = _BARE_WORD.match(payload, index)
        if word:
            token = word.group(0)
            end = word.end()
            separate()
            if _COLON_NEXT.match(payload, end):
                out.append('"' + token + '"')
                fix("quoted a key that had none")
            elif token in _LITERALS:
                out.append(_LITERALS[token])
                fix("wrote Python literals as JSON")
            else:
                out.append(token if _NUMBER.match(token) else '"' + token + '"')
                if not _NUMBER.match(token):
                    fix("quoted a value that had no quotes")
            value_complete = True
            index = end
            continue

        out.append(char)
        index += 1

    return "".join(out), notes


def _read_call_object(fragment: str):
    """(data, notes) for one object of unknown tidiness, (None, notes) if unreadable.

    A payload cut off inside a string is refused here and handled by the caller
    as a dropped call: the missing bytes are content nobody can invent.
    """
    notes: list[str] = []
    opened = _open_object(fragment)
    if opened is not None and opened[2]:
        return None, notes
    candidate = fragment
    if opened is not None:
        closers = opened[1]
        candidate = _TRAILING_COMMA.sub("", candidate.rstrip()) + closers
        notes.append(f"added the missing {closers}")

    for attempt in (candidate, _STRAY_BACKSLASH.sub(r"\\\\", candidate)):
        try:
            return json.loads(attempt), notes
        except (ValueError, TypeError):
            continue

    normalized, more = _normalize_json(candidate)
    try:
        return json.loads(normalized), notes + [m for m in more if m not in notes]
    except (ValueError, TypeError):
        return None, notes + more


def _repair(fragment: str):
    """Close what the model left open: returns (text, what was done) or None.

    Only the two inferable shapes — brackets never closed, and the trailing
    comma that makes an otherwise valid payload unreadable.
    """
    opened = _open_object(fragment)
    if opened is not None and opened[2]:
        return None
    fixed = fragment
    notes = []
    if opened is not None:
        closers = opened[1]
        fixed = _TRAILING_COMMA.sub("", fixed.rstrip()) + closers
        notes.append(f"added the missing {closers}")
    without_commas = _COMMA_BEFORE_CLOSER.sub(r"\1", fixed)
    if without_commas != fixed:
        fixed = without_commas
        notes.append("dropped a trailing comma")
    if not notes:
        return None
    return fixed, " + ".join(notes)


def _describe_drop(fragment: str) -> str:
    """What arrived broken, in the words the model gets back."""
    name = _TOOL_NAME.search(fragment)
    tool = name.group(1) if name else "tool"
    field = ""
    for match in _OPEN_KEY.finditer(fragment):
        field = match.group(1)
    if _open_object(fragment) and field:
        return (f'the "{tool}" call was cut off inside "{field}" after '
                f"{len(fragment)} characters")
    return f'the "{tool}" call could not be read as JSON'


def _args_of(data: dict) -> dict:
    """The argument object, wherever the model put it and however it wrapped it."""
    for key in _ARGS_KEYS:
        if key not in data:
            continue
        value = data[key]
        if isinstance(value, dict):
            return value
        if isinstance(value, str) and value.strip():
            try:
                parsed = json.loads(value)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                return parsed
            return {"input": value}
        if isinstance(value, list):
            return {"input": value}
        return {}
    # Flattened call: {"tool": "read", "path": "a.py"} — everything that is not
    # the tool name is an argument.
    return {k: v for k, v in data.items() if k not in _TOOL_KEYS}


def _calls_from(data) -> list[ParsedCommand]:
    """Every call inside one decoded value, whatever envelope it wrapped itself in."""
    if isinstance(data, list):
        found: list[ParsedCommand] = []
        for item in data:
            found.extend(_calls_from(item))
        return found
    if not isinstance(data, dict):
        return []

    # The API's own envelope, copied out of documentation.
    for envelope in ("tool_calls", "calls", "functions"):
        if isinstance(data.get(envelope), list):
            return _calls_from(data[envelope])
    function = data.get("function")
    if isinstance(function, dict) and any(k in function for k in _TOOL_KEYS):
        merged = dict(function)
        for key in _ARGS_KEYS:
            if key in data:
                merged[key] = data[key]
        return _calls_from(merged)

    tool = next((str(data[key]).strip() for key in _TOOL_KEYS
                 if isinstance(data.get(key), str) and str(data[key]).strip()), "")
    if not tool or tool.lower() in _TOOL_KEYS:
        return []
    canonical = any(key in data for key in ("tool", "tool_name"))
    named_args = any(key in data for key in _ARGS_KEYS)
    leftovers = {k: v for k, v in data.items() if k not in _TOOL_KEYS}
    if not named_args and (not leftovers or not canonical):
        # {"name": "read", "description": ...} is a catalog entry echoed back.
        # Only the "tool" spelling may carry its arguments as loose keys.
        return []
    return [ParsedCommand(tool=tool, args=_args_of(data))]


class CommandParser:

    def parse(self, response: str) -> ParsedResponse:
        commands: list[ParsedCommand] = []
        cuts: list[tuple[int, int]] = []
        suspect: list[str] = []
        repaired: list[str] = []
        dropped: list[str] = []

        def remember(notes) -> None:
            for note in notes:
                if note not in repaired:
                    repaired.append(note)

        # 1. Any JSON object naming a tool: fenced, half-fenced, or bare.
        for start, end in _json_spans(response):
            fragment = response[start:end]
            data, notes = _read_call_object(fragment)
            found = _calls_from(data) if data is not None else []
            if not found:
                # Valid JSON that is not a call stays in the answer: a model
                # showing an example must not have its sample eaten.
                if data is None and _TOOL_MENTION.search(fragment):
                    suspect.append(fragment)
                continue
            commands.extend(found)
            remember(notes)
            cuts.append((_fence_before(response, start), _fence_after(response, end)))

        remaining = response
        for cut_start, cut_end in sorted(cuts, reverse=True):
            remaining = remaining[:cut_start] + remaining[cut_end:]

        # 2. The XML-style tag fallback, for models that answer in tags. The
        #    tool is named by the tag, so the body is the arguments themselves.
        for match in _TAG_PATTERN.finditer(remaining):
            body = match.group(2).strip()
            tool_name = match.group(1)
            args: dict = {}
            if body[:1] in "{[":
                data, notes = _read_call_object(body)
                if data is not None:
                    inner = _calls_from(data)
                    if inner:
                        tool_name = inner[0].tool or tool_name
                        args = inner[0].args
                    elif isinstance(data, dict):
                        args = _args_of(data)
                    remember(notes)
            if not args:
                args = self._parse_simple_args(body)
            commands.append(ParsedCommand(tool=tool_name, args=args))
            remaining = remaining.replace(match.group(0), "", 1)

        # 3. What is left that names a tool: brackets never closed, a stray
        #    comma, a value written across raw newlines. Complete what is
        #    inferable and run it; refuse the rest, but never show it either.
        opened = _open_object(remaining)
        tail = remaining[opened[0]:] if opened else ""
        for fragment, is_tail in [(f, False) for f in suspect] + ([(tail, True)] if tail else []):
            if not _TOOL_MENTION.search(fragment):
                continue
            fixed = _repair(fragment)
            found: list[ParsedCommand] = []
            if fixed:
                data, notes = _read_call_object(fixed[0])
                found = _calls_from(data) if data is not None else []
                if found:
                    remember([fixed[1]] + notes)
            start = remaining.find(fragment)
            if is_tail and start >= 0:
                remaining = remaining[:_fence_before(remaining, start)]
            else:
                remaining = remaining.replace(fragment, "", 1)
            if found:
                commands.extend(found)
            else:
                note = _describe_drop(fragment)
                if note not in dropped:
                    dropped.append(note)

        text_parts = [line.rstrip() for line in remaining.strip().split("\n") if line.strip()]
        clean_text = "\n".join(text_parts)

        return ParsedResponse(
            text=clean_text,
            commands=commands,
            has_commands=len(commands) > 0,
            repaired=repaired,
            dropped=dropped,
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
            "You can use these tools. Respond with EXACTLY one JSON code block:",
            "",
            '```json',
            '{"tool": "tool_name", "args": {"param": "value"}}',
            '```',
            "",
            "Writing the JSON:",
            "- One call per block. For two steps, send one, wait, then send the other.",
            '- A value is one JSON string: write newlines inside it as \\n. Never press '
            "Enter in the middle of a quoted value.",
            "- Escape a backslash as \\\\ and a quote as \\\" — paths included.",
            "- Keep a `content` value under about 200 lines; write the first part, "
            "then add to it with `edit`.",
            "- Name the tool exactly as it is spelled below, and put every parameter "
            "inside \"args\".",
            "",
        ]

        for tool in tools:
            params_desc = []
            props = tool.get("parameters", {}).get("properties", {})
            required = tool.get("parameters", {}).get("required", [])

            for pname, pinfo in props.items():
                kind = pinfo.get("type", "")
                req = " (required)" if pname in required else ""
                label = f"{pname}: {kind}".strip(": ").strip() if kind else pname
                params_desc.append(f"    - {label} — {pinfo.get('description', '')}{req}")

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
