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
from prompt_toolkit.shortcuts import radiolist_dialog, yes_no_dialog
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
    available_models, available_providers, provider_choices, model_choices,
    AVAILABLE_MODES, THEMES,
    catalog_choices, skill_choices, mcp_choices, skin_choices, history_body,
)
from beeagent.ui.viewer import show_scrolled
from beeagent.core.session import Session
from beeagent.core import autosave

PROMPT = HTML('<ansigreen><b>🐝 &gt;</b></ansigreen> ')

# Events that mean "a turn is on the table": the answer landed, a tool answered,
# or the user cut the run short. Not `stream_delta` — a checkpoint per token on
# phone flash is its own kind of outage (see core/autosave.py).
_CHECKPOINT_EVENTS = ("tool_end", "done", "stopped", "response")

# The saver for the REPL run in progress. Module-level because agent events arrive
# through `handle_callback`, which is reached from a worker thread and has no
# context object of its own; None outside a REPL run (a one-shot `--prompt` asks
# nothing of it, and the TUI keeps its own).
_AUTOSAVER: autosave.AutoSaver | None = None

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


def _say(icon: str, text: str):
    """The same note when the string was already translated by whoever sent it."""
    console.print(Text.assemble(f"  {icon} ", Text(text, style="dim")))


def _autosave_checkpoint(event: str):
    """Checkpoint after a completed turn, and say once if the disk said no.

    Swallowed whole: the autosave is the thing that catches a crash, so it must
    never be the thing that causes one. A write that fails is already reported by
    `AutoSaver` itself, once per session, in the conversation's own language.
    """
    saver = _AUTOSAVER
    if saver is None or event not in _CHECKPOINT_EVENTS:
        return
    try:
        saver.checkpoint(reason=event)
    except Exception:
        pass


def _autosave_flush():
    """Write a coalesced turn the moment the REPL stops being busy."""
    try:
        if _AUTOSAVER is not None:
            _AUTOSAVER.flush(reason="idle prompt")
    except Exception:
        pass


def _make_key_bindings() -> KeyBindings:
    kb = KeyBindings()

    @kb.add("f2")
    def _peek_thinking(event):
        # Open the last 10 lines of the model's reasoning; keep streaming.
        get_stream().peek()

    return kb


def agent_callback(agent):
    """The REPL's event handler, with plugin listeners hanging off it.

    Plugins subscribe through `api.event(...)` and never patch the UI: the same
    stream of events the terminal draws from is handed to them afterwards, and a
    listener that throws cannot break the answer it was watching.
    """
    from beeagent.ext.api import emit

    def callback(event: str, data: dict):
        handle_callback(event, data)
        registry = getattr(getattr(agent, "plugins", None), "extensions", None)
        if registry is not None:
            emit(registry, event, data)
        # The skin host sees the same event the extensions do. Imported here, not
        # at module top: nothing slow or networked belongs in the boot path.
        from beeagent.core import skins

        skins.post(event, data)

    return callback


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

    elif event == "stopped":
        _note("🛑",
              f"stopped by you after {data.get('turn', 0)} step(s) — nothing is lost, "
              f"the session and /tasks are intact",
              f"остановлено тобой после {data.get('turn', 0)} шаг(ов) — ничего не потеряно, "
              f"сессия и /tasks на месте")

    elif event == "context_trimmed":
        # No promise that the task "stays in view": a clipped message stays in
        # view and is still missing its middle, which is what the user reads as
        # being lied to. Say which part fell out and where the real text is.
        _note("✂",
              f"history did not fit the context: {data.get('dropped')} oldest message(s) are "
              f"now only a digest line in the system prompt — the model does not read them "
              f"and may contradict them; long outputs are clipped too. /history shows the "
              f"full text",
              f"история не влезла в контекст: {data.get('dropped')} старых сообщений теперь — "
              f"лишь строка конспекта в системном промпте, модель их не читает и может им "
              f"противоречить; большие выводы ещё и обрезаются. Полный текст — в /history")

    elif event == "tool_start":
        stream.on_tool_start()
        render_tool_start(data["tool"], data["args"])

    elif event == "tool_end":
        render_tool_end(data["tool"], data["args"], data["output"], data["error"])

    elif event == "nudged":
        _note("🐝", "the model promised a step but sent no tool call — asked it to act",
              "модель пообещала шаг и не вызвала инструмент — прошу её действовать")

    elif event == "tool_renamed":
        _note("🔧", f"tool name corrected: {data['from']} → {data['to']}",
              f"имя инструмента поправлено: {data['from']} → {data['to']}")

    elif event == "tool_repaired":
        _note("🩹", "incomplete tool call repaired: " + ", ".join(data["notes"])
              + " — the model was told the right shape",
              "неполный вызов починен: " + ", ".join(data["notes"])
              + " — модели показали правильный формат")

    elif event == "tool_dropped":
        # Not repaired: the bytes never arrived, so nothing was run and the
        # model is asked for the call again.
        _note("✂️", "a tool call arrived cut off and was not run — asking again: "
              + ", ".join(data["notes"]),
              "вызов инструмента пришёл обрезанным, ничего не запущено — прошу заново: "
              + ", ".join(data["notes"]))

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

    elif event == "provider_fallback":
        # g4f is optional and a fresh Termux install leaves it out, so the pool
        # takes the question. Say it happened, and say the one command left to do.
        if data.get("seat"):
            _note("🐝", "no g4f on this machine — answering through the pool instead",
                  "g4f на этой машине не ставится — отвечаем через пул")
        else:
            _note("🐝", "no g4f on this machine — take a seat in the pool: /pool enroll",
                  "g4f на этой машине не ставится — возьми место в пуле: /pool enroll")

    elif event == "economy_hit":
        render_economy_hit()

    elif event == "waiting":
        _note("⌛", f"still waiting for the model… {data.get('seconds')}s",
              f"всё ещё жду модель… {data.get('seconds')}с")

    elif event == "retry":
        _note("🔁", f"the bee is retrying ({data.get('attempt')}/3)...",
              f"пчела повторяет попытку ({data.get('attempt')}/3)...")

    elif event == "stream_reset":
        # The buffers go, and the user hears it go: a fragment already printed
        # cannot be unprinted, so the retry has to be announced or the next
        # answer reads as a duplicate.
        _note("🗑", "the unfinished answer was thrown away, not appended — the next try "
                    "starts on a clean line",
              "недописанный ответ выброшен, а не дописан — следующая попытка начнётся "
              "с чистой строки")
        stream.on_reset()

    elif event == "error":
        stream.on_error(data["message"])

    # Last, after the screen is done with the event: a turn that finished is a turn
    # worth having on disk, and the drawing must not wait on it.
    _autosave_checkpoint(event)


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
def _current_skin() -> str:
    """The skin in force, spelled the way the picker's first row is spelled."""
    from beeagent.core import skins

    active = skins.active_name()
    return active if active != skins.BASELINE else "off"


def _picker_specs(ctx: ReplContext):
    # The provider belongs in the title. A person who ran `/provider crax` and was
    # refused -- no key -- stays on g4f, and an unlabelled list of g4f model names
    # reads as "these are crax's models", which is how one real session ended with
    # the conclusion that the model list ignores the provider.
    provider = getattr(ctx.config, "provider", "") or "g4f"
    model_title = L(f"🐝 Select model — provider: {provider}"
                    " — ★ recommended, widest context first",
                    f"🐝 Выбор модели — провайдер: {provider}"
                    " — ★ рекомендуемые, сначала с большим контекстом")
    return {
        "models":    (model_title,
                     lambda: model_choices(ctx), "/models",
                     lambda: ctx.config.model),
        "model":     (model_title,
                     lambda: model_choices(ctx), "/models",
                     lambda: ctx.config.model),
        # One door: both names open the same picker, and the chosen value comes
        # back through `/providers`, where the editing sub-commands live too.
        # The extra row `add` is how a mouse user reaches the guided form.
        "providers": (L("🐝 Select provider — add a new one from the last row",
                        "🐝 Выбрать провайдера — добавить своего можно последней строкой"),
                      lambda: provider_choices(ctx), "/providers",
                      lambda: ctx.config.provider),
        "provider":  (L("🐝 Select provider — add a new one from the last row",
                        "🐝 Выбрать провайдера — добавить своего можно последней строкой"),
                      lambda: provider_choices(ctx), "/providers",
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
        # The skins board in a dialog: the first row is BeeCode's own interface, so
        # "take it off" is a click like everything else. Chosen values come back as
        # `/skins <value>`, which is the same command a keyboard user types.
        "skins":     (L("🐝 Select skin", "🐝 Выбрать скин"),   lambda: skin_choices(ctx),        "/skins",
                      _current_skin),
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
    line = f"{apply_cmd} {choice}"
    parts = line.split()
    guided = await try_guided(ctx, parts[0][1:], parts[1:])
    if guided is not None:
        return guided
    return dispatch(ctx, line)


#: `/providers` sub-commands that need to ask the person something. Everything
#: else about providers — switching, `models <name> <a,b>`, `remove` — is a
#: complete sentence and belongs to `dispatch`; an interview is not.
_GUIDED_SUBS = ("add", "new", "create", "edit", "set", "change", "key")


async def try_guided(ctx: ReplContext, name: str, args: list[str]):
    """Run the guided endpoint form for the classic REPL, when a line needs one.

    prompt_toolkit dialogs must be awaited — the blocking `.run()` refuses to
    start inside the loop that already owns the terminal — so this cannot live
    behind the synchronous `dispatch`; the command path calls it first and only
    falls through to `dispatch` when there is nothing to ask. The full-screen
    TUI does not come this way: the same command there pushes the modal screen
    from `ui/provider_form.py` itself.
    """
    if name not in ("providers", "provider") or not args:
        return None
    sub = args[0].lower()
    if sub not in _GUIDED_SUBS:
        return None
    adding = sub in ("add", "new", "create")
    keys_only = sub == "key"
    rest = args[1:]
    if not adding and not rest:
        return None                     # `/providers edit` alone: dispatch asks for a name
    if keys_only and len(rest) > 1:
        return None                     # the pool is given inline: no interview needed
    from beeagent.ui import provider_form

    try:
        return await provider_form.guided_form(
            ctx, name=rest[0] if rest else "", adding=adding, keys_only=keys_only,
            workdir=ctx.agent.workdir if ctx.agent is not None else ".")
    except Exception as e:
        # A broken dialog must not take the conversation with it; say what
        # failed and let the plain command answer.
        console.print(Text(L(f"  the form could not run: {e.__class__.__name__}: {e}",
                             f"  форма не запустилась: {e.__class__.__name__}: {e}"),
                           style="dim"))
        return None


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
        agent.run(line, session=ctx.session, callback=agent_callback(agent))
    )
    _ACTIVE_TASK.add_done_callback(_on_done)


def _stop_active_task():
    global _ACTIVE_TASK
    if _ACTIVE_TASK is not None and not _ACTIVE_TASK.done():
        _ACTIVE_TASK.cancel()
        _ACTIVE_TASK = None
    get_stream().reset()


async def _offer_update(agent) -> bool:
    """Ask once whether to pull a newer BeeCode. True means the question is settled.

    The version check runs in a worker thread, so this returns False — and asks
    again on the next round — until the thread has an answer. The prompt is
    never held up waiting for GitHub.
    """
    from beeagent import __version__
    from beeagent.core import updater

    workdir = getattr(agent, "workdir", ".") or "."
    if not updater.wait(0):
        return False
    latest = updater.available(workdir)
    if not latest:
        return True
    answer = await yes_no_dialog(
        title=brand_ramp(f" 🐝 BeeCode {latest} "),
        text=L(f"A newer BeeCode is out: {latest}, you are running {__version__}.\n"
               "It needs a restart either way.\n\nUpdate now?",
               f"Вышел новый BeeCode: {latest}, у тебя стоит {__version__}.\n"
               "Перезапуск нужен в любом случае.\n\nОбновить сейчас?"),
        yes_text=L("Update", "Обновить"),
        no_text=L("Later", "Позже"),
        style=BEE_DIALOG_STYLE,
    ).run_async()
    if not answer:
        # A "no" is about this release, not about ever hearing again: the next
        # version asks afresh.
        updater.write_cache(workdir, declined=latest)
        _note("🐝", "later then — /update does it whenever you want",
              "как хочешь — /update сделает это когда скажешь")
        return True
    await _run_update()
    return True


async def _run_update() -> None:
    """Run the updater off the loop and say what came of it."""
    from beeagent.cli import update_self

    _note("⬇️", "updating BeeCode… this takes a minute", "обновляю BeeCode… это минута")
    try:
        code = await asyncio.to_thread(update_self)
    except Exception as e:
        code = -1
        _note("❌", f"the updater itself failed: {e}", f"сам обновлятор упал: {e}")
    if code == 0:
        _note("✅", "updated — close this window and start `beecode` again",
              "готово — закрой окно и запусти `beecode` заново")
    elif code != -1:
        _note("❌", "the update did not finish — /doctor says what is wrong",
              "обновление не дошло — /doctor скажет, что не так")


async def _ask_route(agent, error):
    """Ask what to do about a per-IP limit. Only the user knows their options.

    A daily allowance is not asked about: waiting does not fix it, and the
    provider already moved to the next key. An IP limit is different — it lifts
    by itself in seconds, or by leaving through another address — so the question
    is worth putting on screen, with the command shown before anything runs it.
    """
    import subprocess

    from beeagent.i18n import L

    config = getattr(agent, "config", None)
    wait = int(getattr(error, "retry_after", 0) or 0) or 60
    command = (getattr(config, "vpn_command", "") if config else "").strip()

    if command:
        answer = await yes_no_dialog(
            title=brand_ramp(" 🐝 лимит по IP "),
            text=L(f"crax-gpt stopped answering this IP address. Waiting {wait}s works, "
                   f"but you can change the address with your own command:\n\n    {command}\n\n"
                   "BeeCode runs it and repeats the request. Agree?",
                   f"crax-gpt перестал отвечать на этот IP. Подождать {wait} с — рабочий вариант, "
                   f"но можно сменить адрес своей командой:\n\n    {command}\n\n"
                   "BeeCode её запустит и повторит запрос. Согласен?"),
            yes_text=L("Run it", "Запустить"), no_text=L("Just wait", "Просто подождать"),
            style=BEE_DIALOG_STYLE).run_async()
        if not answer:
            return "wait"
        _note("🔌", f"running: {command}", f"выполняю: {command}")
        try:
            done = await asyncio.to_thread(subprocess.run, command, shell=True,
                                           capture_output=True, text=True, timeout=90)
            if done.returncode != 0:
                _note("⚠️", f"the command exited with {done.returncode}: "
                            f"{(done.stderr or done.stdout or '').strip()[:120]}",
                      f"команда завершилась кодом {done.returncode}: "
                      f"{(done.stderr or done.stdout or '').strip()[:120]}")
        except Exception as e:
            _note("⚠️", f"the command did not run: {e}", f"команда не запустилась: {e}")
        return "wait" if wait else None

    answer = await yes_no_dialog(
        title=brand_ramp(" 🐝 лимит по IP "),
        text=L(f"crax-gpt is limiting this IP address. Nothing is wrong with BeeCode — "
               f"the endpoint asks for {wait}s of quiet.\n\nWait and repeat the request?",
               f"crax-gpt ограничил этот IP. С BeeCode всё в порядке — эндпоинт просит "
               f"тишины {wait} с.\n\nПодождать и повторить запрос?"),
        yes_text=L("Wait", "Подождать"), no_text=L("Stop", "Отменить"),
        style=BEE_DIALOG_STYLE).run_async()
    return "wait" if answer else None


async def run_repl(agent, config, session=None):
    from beeagent.core import updater
    from beeagent.core.session import Session

    ctx = ReplContext(agent=agent, config=config, session=session or Session())
    updater.start(getattr(agent, "workdir", ".") or ".")
    asked_about_update = False
    # The provider asks the user about a rate limit only through this hook.
    agent.route_question = lambda error: _ask_route(agent, error)

    global _AUTOSAVER
    workdir = str(getattr(agent, "workdir", ".") or ".")
    _AUTOSAVER = autosave.AutoSaver(
        workdir=workdir, session=ctx.session,
        on_message=lambda text: _say("·", text))
    # Follow the live session: `/continue` and `/reset` replace the object under us,
    # and the one left behind is finished with, not lost.
    _AUTOSAVER.attach(lambda: ctx.session)
    # Rule 2 — say it here, before the first prompt, because the alternative is
    # that the user finds out by noticing the history is short.
    try:
        notice = autosave.recovered_notice(workdir, current_id=ctx.session.session_id)
        if notice:
            _say("🐝", notice)
    except Exception:
        pass

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
            if not asked_about_update:
                asked_about_update = await _offer_update(agent)
            # The debounce holds a finished turn back while a burst of cheap steps
            # is arriving. About to wait on a human is the end of the burst: write
            # what is pending, so an idle session on disk is never behind the
            # screen by more than the turn actually in flight.
            if _AUTOSAVER is not None and _AUTOSAVER.pending():
                _autosave_flush()
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
                # Three doors, one order: an interview the line cannot answer on
                # its own (the endpoint form), then the mouse picker for a bare
                # list command, then the plain command.
                res = await try_guided(ctx, name, args)
                if res is None:
                    res = await try_picker(ctx, name, args)
                if res is None:
                    res = dispatch(ctx, line)
                if res.action == "clear":
                    console.clear()
                if res.action == "stop":
                    # The same path Ctrl+C takes, so there is one way to stop and
                    # not two that disagree about what they cancel.
                    _stop_active_task()
                if res.output is not None:
                    console.print(res.output)
                if res.action == "update":
                    await _run_update()
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
        # The clean save, the announcement when it does not land, and the sweep of
        # old session files — all of it before the plugins go, so a shutdown that
        # hangs cannot swallow the one line saying what was deleted.
        try:
            if _AUTOSAVER is not None:
                _AUTOSAVER.close(ctx.session)
            else:
                ctx.session.save()
        except Exception as e:
            _say("⚠", L(f"the session could not be saved ({e.__class__.__name__}: {e})",
                        f"сессия не сохранена ({e.__class__.__name__}: {e})"))
        finally:
            _AUTOSAVER = None
        try:
            agent.plugins.shutdown()      # stop live MCP server processes
        except Exception:
            pass
