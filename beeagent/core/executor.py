"""One turn's worth of tool calls: look up, gate, run, report.

This used to be the middle of `Agent.run()` (`core/agent.py`), sharing a
300-line loop body with message building, cache checks and answer handling.
It moves here unchanged in behaviour: each parsed command is resolved
(aliases, then typo repair), fitted to the tool's parameters, passed through
the permission gate, executed in a worker thread, and its result appended to
the session — with a callback at every step for the UI.

The agent owns the interrupt handle: `agent.running_tool` names the tool in
flight so the cancel handler in `run()` can kill it (a bash command left
orphaned writes files indefinitely after the loop has stopped).
"""
import asyncio

from beeagent.tools.ask import AskTool
from beeagent.tools.base import ToolResult


def _ask_user(callback, question: str) -> str:
    """Pause the tool loop and ask the user a question.

    This runs inside ``asyncio.to_thread`` (a worker thread) and reads from
    the real terminal via ``input()`` — no event-loop gymnastics needed.
    """
    try:
        import sys

        # The callback announces the question to the UI layer (plugin/skin)
        # so the question is visible even when input() blocks below.
        if callable(callback):
            callback("ask", {"question": question})

        print(f"\n[ASK] {question}")
        sys.stdout.flush()
        answer = input("> ").strip()
        return answer or "(user did not answer)"
    except (EOFError, KeyboardInterrupt):
        return "(user did not answer)"


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
        import json

        return ToolResult(output=json.dumps(raw, ensure_ascii=False), error=False)
    return ToolResult(output=str(raw), error=False)


async def execute_commands(agent, session, parsed, callback) -> None:
    """Run every parsed command; feed each result back into the session."""
    tools = agent.tools
    permissions = agent.permissions
    for cmd in parsed.commands:
        asked = cmd.tool
        tool = tools.get(asked)

        if tool is not None:
            # Aliases ("read_directory") are accepted but the UI and
            # the history speak the canonical name.
            canonical = tools.canonical_name(asked)
            if canonical != asked:
                cmd.tool = canonical
                tool = tools.get(canonical)
                if callback:
                    callback("tool_renamed", {"from": asked, "to": canonical})

        if tool is None:
            # Models mistype names ("reed" for "read"); a near
            # exact match is safe to run, a guess is not.
            near = tools.resolve(cmd.tool)
            if near is not None:
                if callback:
                    callback("tool_renamed", {"from": cmd.tool, "to": near})
                cmd.tool = near
                tool = tools.get(near)

        if tool is None:
            names = ", ".join(tools.list_names())
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
        if not permissions.allows(tool):
            message = permissions.refusal(tool)
            session.add_tool_result(f"[tool result] {message}")
            permissions.denied_this_run.add(cmd.tool)
            if callback:
                callback("tool_denied", {
                    "tool": cmd.tool, "args": args, "message": message,
                })
            continue

        # The tool may behave differently for a granted run (see
        # BaseTool.granted); the gate above already refused otherwise.
        tool.granted = permissions.mode == "auto" or cmd.tool in permissions.granted
        silent = bool(getattr(tool, "silent", False))
        if callback and not silent:
            callback("tool_start", {"tool": cmd.tool, "args": args})

        ask_tool = tool if isinstance(tool, AskTool) else None
        if ask_tool:
            ask_tool.on_ask = lambda q: _ask_user(callback, q)

        try:
            agent.running_tool = tool
            # Tools are sync (subprocess, MCP, file IO); running them
            # in a worker thread keeps the prompt and stream alive.
            raw = await asyncio.to_thread(tool.execute, **args)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            raw = ToolResult(output=f"ERROR: {e}", error=True)
        finally:
            agent.running_tool = None

        # All of the bookkeeping is inside one guard, and the result is
        # fitted to the ToolResult shape first: an extension that
        # answers with a bare string has run its call, and reading
        # `.output` off it here used to raise out of run() *after* the
        # assistant row carrying the call was written — a transcript
        # ending on a call with no result, replayed by every later turn.
        try:
            result = _as_tool_result(raw)
            output = result.output if result.output.strip() else "(empty output)"
        except asyncio.CancelledError:
            raise
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

        if not result.error and cmd.tool != "think":
            # Movement the plan HUD can show: working calls since the plan.
            from beeagent.tools.think import ThinkTool

            ThinkTool.note_work()
        if callback:
            # Silent tools draw instead of announcing: the render payload
            # travels on the same event, so no new event name, no skin drift.
            event = dict(tool=cmd.tool, args=args, output=output,
                         error=result.error)
            if silent and not result.error:
                event["render"] = result.metadata
            callback("tool_end", event)
