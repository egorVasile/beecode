"""Interactive REPL with a live slash-command palette.

Typing '/' opens a completion menu below the prompt that filters as you type.
Commands can take arguments that are themselves completed from live sources
(models, providers, modes, saved sessions).

While the agent is busy the prompt stays available: new messages are parked
in agent.pending and delivered along with the agent's next model call.
"""
from __future__ import annotations

import asyncio

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.patch_stdout import patch_stdout
from prompt_toolkit.shortcuts import radiolist_dialog
from prompt_toolkit.styles import Style
from rich.text import Text

from beeagent.i18n import L, get_lang

from beeagent.ui.components import (
    console, brand_ramp,
    get_stream, show_thinking_fallback, thinking_body,
    render_tool_start, render_tool_end,
    render_error, render_economy_hit,
    render_tool_denied, render_model_switched,
)
from beeagent.ui.commands import (
    ReplContext, build_sources, get_suggestions, dispatch,
    available_models, available_providers, model_choices, AVAILABLE_MODES, THEMES,
    catalog_choices, skill_choices, mcp_choices, history_body,
)
from beeagent.ui.viewer import show_scrolled
from beeagent.core.session import Session

PROMPT = HTML('<ansigreen><b>🐝 &gt;</b></ansigreen> ')

BEE_STYLE = Style.from_dict({
    "completion-menu.completion": "bg:#102a12 #c8e6c9",
    "completion-menu.completion.current": "bg:#2e7d32 #ffffff",
    "completion-menu.meta.completion": "bg:#102a12 #6b8f6e",
    "completion-menu.meta.completion.current": "bg:#2e7d32 #d0e8d2",
    "completion-menu": "#c8e6c9 bg:#102a12",
})

# Picker dialog in the BeeCode palette: dark hive backdrop, honey title,
# green frame. Same accent colors as the banner gradient in components.py.
BEE_DIALOG_STYLE = Style.from_dict({
    # full-screen backdrop behind the modal — deep hive green, never blue
    "dialog": "bg:#08170a",
    "shadow": "bg:#030c04",
    "dialog.body": "bg:#0d2410 #eafbe7",
    # frame + title
    "frame": "bg:#0d2410",
    "frame.border": "#43a047",
    "frame.label": "bold #ffcc00",
    # radio rows
    "radio-list": "bg:#0d2410",
    "radio": "#eafbe7",
    "radio-selected": "bold bg:#2e7d32 #ffffff",
    "radio-checked": "bold #ffcc00",
    "radio-number": "#7cb342",
    # buttons
    "button": "bg:#16321a #c8e6c9",
    "button.focused": "bg:#43a047 #06130a",
    "button.text": "#eafbe7",
    "button.arrow": "bold #ffcc00",
    # scrollbar
    "scrollbar": "bg:#0d2410",
    "scrollbar.button": "bg:#43a047",
})

_ACTIVE_TASK = None


def _note(icon: str, english: str, russian: str):
    """A dim one-line UI note, in the active language."""
    console.print(Text.assemble(f"  {icon} ", Text(L(english, russian), style="dim")))


def _make_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("f2")
    def _peek_thinking(event):
        # Open the last 10 lines of the model's reasoning; keep streaming.
        get_stream().peek()

    return kb


def handle_callback(event: str, data: dict):
    """Bridge agent events into the streaming UI."""
    stream = get_stream()

    if event == "status":
        stream.on_status()

    elif event == "reasoning_delta":
        stream.on_thinking(data["text"])

    elif event == "stream_delta":
        stream.on_content(data["text"])

    elif event == "done":
        stream.on_done()

    elif event == "response":
        stream.on_response(data["text"])

    elif event == "queued_sent":
        items = data.get("items", [])
        _note("📨",
              f"queued message went out with this step: {[i[:40] for i in items]}",
              f"сообщение из очереди ушло с этим шагом: {[i[:40] for i in items]}")

    elif event == "context_trimmed":
        _note("✂",
              f"history did not fit the context: compressed {data.get('dropped')} messages into "
              f"a summary in the system prompt, big outputs are clipped — the task stays in view",
              f"история не влезла в контекст: сжал {data.get('dropped')} сообщений в конспект "
              f"в системном промпте, большие выводы обрезаю — задачу держу")

    elif event == "tool_start":
        stream.on_tool_start()
        render_tool_start(data["tool"], data["args"])

    elif event == "tool_end":
        render_tool_end(data["tool"], data["args"], data["output"], data["error"])

    elif event == "tool_renamed":
        _note("🔧", f"tool name corrected: {data['from']} → {data['to']}",
              f"имя инструмента поправлено: {data['from']} → {data['to']}")

    elif event == "tool_unknown":
        # Recoverable: the model gets the real tool list back and continues.
        _note("🤔", f"unknown tool “{data['tool']}” — showed the model the real list, it continues",
              f"не знаю инструмент «{data['tool']}» — показала модели список, она продолжит")

    elif event == "tool_error":
        render_error(L(f"tool '{data.get('tool')}' failed: {data.get('message')}",
                       f"инструмент '{data.get('tool')}' упал: {data.get('message')}"))

    elif event == "tool_denied":
        render_tool_denied(data.get("tool", ""), data.get("args") or {})

    elif event == "model_switched":
        render_model_switched(data.get("from", ""), data.get("to", ""))

    elif event == "economy_hit":
        render_economy_hit()

    elif event == "waiting":
        _note("⌛", f"still waiting for the model… {data.get('seconds')}s",
              f"всё ещё жду модель… {data.get('seconds')}с")

    elif event == "retry":
        _note("🔁", f"the bee is retrying ({data.get('attempt')}/3)...",
              f"пчела повторяет попытку ({data.get('attempt')}/3)...")

    elif event == "stream_reset":
        stream.on_reset()

    elif event == "error":
        stream.on_error(data["message"])


class BeeCompleter(Completer):
    def __init__(self, ctx: ReplContext):
        self.ctx = ctx

    def get_completions(self, document, complete_event):
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        sources = build_sources(self.ctx)
        for s in get_suggestions(text, sources):
            yield Completion(
                s.text,
                start_position=s.start_position,
                display=s.display,
                display_meta=s.meta,
            )


# Commands that open a mouse-clickable picker when run without an explicit
# argument. Each maps to (dialog title, values callable, command that applies
# the chosen value, callable returning the currently active value).
def _picker_specs(ctx: ReplContext):
    return {
        "models":    (L("🐝 Select model — ★ recommended, widest context first",
                        "🐝 Выбор модели — ★ рекомендуемые, сначала с большим контекстом"),
                     lambda: model_choices(ctx), "/model",
                     lambda: ctx.config.model),
        "model":     (L("🐝 Select model — ★ recommended, widest context first",
                        "🐝 Выбор модели — ★ рекомендуемые, сначала с большим контекстом"),
                     lambda: model_choices(ctx), "/model",
                     lambda: ctx.config.model),
        "providers": (L("🐝 Select provider", "🐝 Выбрать провайдер"), lambda: available_providers(ctx), "/provider",
                      lambda: ctx.config.provider),
        "provider":  (L("🐝 Select provider", "🐝 Выбрать провайдер"), lambda: available_providers(ctx), "/provider",
                      lambda: ctx.config.provider),
        "mode":      (L("🐝 Select mode", "🐝 Выбрать режим"),     lambda: list(AVAILABLE_MODES),      "/mode",
                      lambda: ctx.config.mode),
        "lang":      (L("🐝 Select language", "🐝 Выбрать язык"),  lambda: ["en", "ru"],               "/lang",
                      get_lang),
        "theme":     (L("🐝 Select theme", "🐝 Выбрать тему"),    lambda: list(THEMES),               "/theme",
                      lambda: ctx.theme),
        "sessions":  (L("🐝 Load session", "🐝 Загрузить сессию"),    lambda: Session.list_sessions(),    "/continue",
                      lambda: ctx.session.session_id if ctx.session is not None else None),
        "continue":  (L("🐝 Load session", "🐝 Загрузить сессию"),    lambda: Session.list_sessions(),    "/continue",
                      lambda: ctx.session.session_id if ctx.session is not None else None),
        "plugins":   (L("🐝 Catalog: install", "🐝 Каталог: установить"), lambda: catalog_choices(ctx),    "/plugin install",
                      lambda: None),
        "plugin":    (L("🐝 Catalog: install", "🐝 Каталог: установить"), lambda: catalog_choices(ctx),    "/plugin install",
                      lambda: None),
        "skills":    (L("🐝 Skill", "🐝 Скил"),            lambda: skill_choices(ctx),         "/skill",
                      lambda: None),
        "skill":     (L("🐝 Skill", "🐝 Скил"),             lambda: skill_choices(ctx),         "/skill",
                      lambda: None),
        "mcp":       (L("🐝 MCP server", "🐝 MCP-сервер"),      lambda: mcp_choices(ctx),           "/mcp tools",
                      lambda: None),
    }


async def try_picker(ctx: ReplContext, name: str, args: list[str]):
    """Show a mouse-driven picker for list commands.

    Returns a CommandResult when a value was chosen and applied, or None when
    the picker does not apply / the user cancelled (so the caller can fall back
    to the normal text output).

    The dialog must be awaited: prompt_toolkit's blocking `.run()` creates a
    fresh event loop, which asyncio refuses to start while the REPL loop is
    running (the dialog would silently never appear).
    """
    if args:
        return None
    specs = _picker_specs(ctx)
    if name not in specs:
        return None
    title, values_fn, apply_cmd, current_fn = specs[name]
    try:
        raw = values_fn()
    except Exception:
        return None
    if not raw:
        return None
    # A value is either a bare string or a (value, label) pair.
    pairs = [v if isinstance(v, tuple) else (v, v) for v in raw]
    current = current_fn()
    default = current if current in [value for value, _ in pairs] else None
    try:
        choice = await radiolist_dialog(
            title=brand_ramp(title),
            text=L("Click with the mouse, or ↑↓ + Enter.", "Клик мышью, или ↑↓ + Enter."),
            values=pairs,
            default=default,
            ok_text="Select",
            cancel_text="Cancel",
            style=BEE_DIALOG_STYLE,
        ).run_async()
    except KeyboardInterrupt:
        return None              # Ctrl+C cancels the dialog, keeps the REPL
    except Exception:
        return None
    if choice is None:
        return None
    return dispatch(ctx, f"{apply_cmd} {choice}")


def _spawn_agent_task(agent, line: str, ctx: ReplContext):
    """Run the agent as a background task; callbacks render everything.

    Returns immediately so the prompt stays available while the agent works.
    """
    global _ACTIVE_TASK

    def _on_done(fut):
        try:
            fut.result()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            handle_callback("error", {"message": str(e)})

    get_stream().reset()
    # Set synchronously so a fast next input is queued, not started in parallel.
    agent.is_busy = True
    _ACTIVE_TASK = asyncio.create_task(
        agent.run(line, session=ctx.session, callback=handle_callback)
    )
    _ACTIVE_TASK.add_done_callback(_on_done)


def _stop_active_task():
    global _ACTIVE_TASK
    if _ACTIVE_TASK is not None and not _ACTIVE_TASK.done():
        _ACTIVE_TASK.cancel()
        _ACTIVE_TASK = None
    get_stream().reset()


async def run_repl(agent, config, session=None):
    from beeagent.core.session import Session

    ctx = ReplContext(agent=agent, config=config, session=session or Session())

    prompt_session: PromptSession = PromptSession(
        completer=BeeCompleter(ctx),
        complete_while_typing=True,
        # Mouse off on purpose: while the prompt is active, prompt_toolkit
        # turns on xterm mouse reporting, and the terminal then hands the wheel
        # to the completion menu instead of scrolling the conversation back.
        # Pickers and /history bring their own mouse-enabled full-screen apps.
        mouse_support=False,
        style=BEE_STYLE,
        key_bindings=_make_key_bindings(),
    )

    try:
        while ctx.running:
            console.print()
            try:
                # patch_stdout routes agent output above the active prompt
                # instead of clobbering it. raw=True keeps rich's ANSI colors
                # intact (the default escapes them into `[1m ?[0m` garbage).
                with patch_stdout(raw=True):
                    line = (await prompt_session.prompt_async(PROMPT)).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                # Ctrl+C: cancel background work if any, keep the session alive.
                if agent.is_busy:
                    _stop_active_task()
                    console.print(L("  ⏹ stopped", "  ⏹ остановлено"), style="dim")
                    continue
                console.print("\n  [dim]Goodbye! 🐝[/]\n")
                break

            if not line:
                continue
            if line.lower() in ("quit", "exit", "q"):
                if agent.is_busy:
                    _stop_active_task()
                console.print("\n  [dim]Goodbye! 🐝[/]\n")
                break
            if line.startswith("/"):
                parts = line.split()
                name = parts[0][1:]
                args = parts[1:]
                res = await try_picker(ctx, name, args)
                if res is None:
                    res = dispatch(ctx, line)
                if res.action == "clear":
                    console.clear()
                if res.output is not None:
                    console.print(res.output)
                if res.action == "thinking_pager":
                    if not await show_scrolled(L("💭 reasoning", "💭 мысли"), thinking_body()):
                        show_thinking_fallback()
                if res.action == "history_pager":
                    body = history_body(ctx.session)
                    if not await show_scrolled(L("📜 history", "📜 история"), body):
                        console.print(Text(body, overflow="fold"))
                continue

            if agent.is_busy:
                n = agent.pending.put(line)
                _note("📥", f"in the queue ({n}) — the bee takes it with the next step",
                      f"в очереди ({n}) — пчела унесёт с следующим шагом")
                continue

            _spawn_agent_task(agent, line, ctx)
    except KeyboardInterrupt:
        console.print("\n  [dim]Goodbye! 🐝[/]\n")
    finally:
        _stop_active_task()
        ctx.session.save()
        try:
            agent.plugins.shutdown()      # stop live MCP server processes
        except Exception:
            pass
