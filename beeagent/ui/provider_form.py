"""`/providers add`, `/providers edit <name>` and `/providers key <name>` —
one endpoint's four facts on one screen, in whichever screen the user is in.

Name, address, keys, models. That is everything an endpoint is in this program,
and it is what `core/provider_setup.py` writes; this module only draws the boxes
and hands the answer over, so the form, the guided REPL interview and the
one-line `/key` command cannot disagree about what a saved provider means.

The keys field starts empty and *keeps* what is stored when left empty. A form
that printed the saved tokens to prefill them would put the secrets on a screen
that is often recorded, mirrored or read over a shoulder — and the whole reason
a person has several keys is that one of them gets used up. Every echo of a key
here goes through `provider_setup.mask_key`: the last four characters, never
more.

Two entry points, one per interface:
  * `open_form(ctx, name, ...)` — the Textual modal screen, used when a command
    is dispatched inside the running Textual app (`beeagent/ui/tui.py` itself
    needs no change: the screen comes from this file and is pushed onto the
    running app).
  * `guided_form(ctx, name, ...)` — the awaited prompt_toolkit interview for
    the classic REPL, driven from `beeagent/ui/repl.py` because `dispatch` is
    synchronous and a dialog must be awaited there.
"""
import asyncio

from textual import work
from textual.app import Binding, ComposeResult
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label

from beeagent.core import provider_setup
from beeagent.i18n import L
from beeagent.ui.components import HONEY, LEAF, console

HIVE_PANEL = "#2b2113"
HIVE_BACKDROP = "#0b0a07"

#: (widget id, label, placeholder) in the order Tab walks them.
FIELDS = (
    ("pf-name", L("name", "имя"), L("groq", "groq")),
    ("pf-url", L("base url", "base url"), "https://api.example.com/v1"),
    ("pf-keys", L("api keys, comma separated", "api ключи, через запятую"),
     L("leave empty to keep the stored keys", "пусто — оставить сохранённые ключи")),
    ("pf-models", L("models, comma separated", "модели, через запятую"),
     L("empty asks the endpoint for its list", "пусто — спросить у эндпоинта")),
)


class ProviderForm(ModalScreen):
    """The question an endpoint answers to: where it is, who may ask, what it has."""

    #: Textual's ModalScreen ships no escape of its own, and a form with four
    #: boxes on a live config needs a way out that is on the screen: a person who
    #: opened it by mistake must be able to leave without reaching for the mouse.
    BINDINGS = [
        Binding("escape", "cancel", "Cancel", priority=True),
    ]

    CSS = f"""
    #pf-backdrop {{
        width: 100%;
        height: 100%;
        align: center middle;
        background: {HIVE_BACKDROP};
    }}
    #pf-box {{
        width: 84;
        max-width: 96%;
        height: auto;
        padding: 1 2;
        background: {HIVE_PANEL};
        border: heavy {LEAF};
    }}
    #pf-title {{ width: 1fr; color: {HONEY}; text-style: bold; }}
    #pf-note {{ width: 1fr; color: {LEAF}; }}
    .pf-row {{ width: 1fr; margin-top: 1; }}
    .pf-label {{ width: 26; color: {HONEY}; }}
    .pf-field {{ width: 1fr; }}
    #pf-buttons {{ width: 1fr; margin-top: 1; align-horizontal: right; }}
    #pf-buttons Button {{ margin-left: 2; }}
    """

    def __init__(self, fields: provider_setup.Fields, ctx, workdir: str = ".",
                 keys_only: bool = False):
        super().__init__()
        self.fields = fields
        self.ctx = ctx
        self.workdir = workdir
        self.keys_only = keys_only
        stored = len(fields.key_list)
        self.title = L(f"provider: {fields.name or '(a new one)'}",
                       f"провайдер: {fields.name or '(новый)'}")
        if keys_only:
            self.title = L(f"keys for {fields.name}", f"ключи для {fields.name}")
        self.note = L(
            f"url {fields.url or '—'} · keys stored: {stored} · models: {len(fields.models)}"
            " · Enter saves, Esc leaves everything as it was",
            f"адрес {fields.url or '—'} · ключей сохранено: {stored} · "
            f"моделей: {len(fields.models)} · Enter — сохранить, Esc — ничего не изменено")

    def compose(self) -> ComposeResult:
        with Container(id="pf-backdrop"):
            with Vertical(id="pf-box"):
                yield Label(self.title, id="pf-title")
                yield Label(self.note, id="pf-note")
                for ident, label, placeholder in FIELDS:
                    with Horizontal(classes="pf-row"):
                        yield Label(label, id=f"{ident}-label", classes="pf-label")
                        yield Input(placeholder=placeholder, id=ident,
                                    password=(ident == "pf-keys"), classes="pf-field")
                with Horizontal(id="pf-buttons"):
                    yield Button(L("models from the endpoint", "модели от эндпоинта"),
                                 id="pf-discover", variant="primary")
                    yield Button(L("save", "сохранить"), id="pf-save", variant="success")
                    yield Button(L("cancel", "отмена"), id="pf-cancel", variant="error")

    def on_mount(self) -> None:
        """Prefill what is safe to show. Keys are never shown, stored or not."""
        values = {"pf-name": self.fields.name, "pf-url": self.fields.url,
                  "pf-keys": "",
                  "pf-models": ", ".join(self.fields.models)}
        for ident, value in values.items():
            field = self.query_one(f"#{ident}", Input)
            field.value = value
        self.query_one("#pf-keys" if self.keys_only else "#pf-name", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in any box means the same thing: save.

        The last field's Enter would otherwise be a keystroke that does nothing,
        and a person who has just typed four values should not have to find the
        button with the mouse to keep them. Escape is the screen's own binding,
        which closes with no answer — and nothing is written then.
        """
        self._save()

    def action_cancel(self) -> None:
        self.dismiss("")

    def _collect(self) -> provider_setup.Fields:
        """The four fields as typed, with the stored keys kept when the box is empty."""
        fields = provider_setup.Fields(
            name=self.query_one("#pf-name", Input).value.strip(),
            was=self.fields.name,
            url=self.query_one("#pf-url", Input).value.strip(),
            keys=self.query_one("#pf-keys", Input).value.strip(),
            models=provider_setup.split_models(
                self.query_one("#pf-models", Input).value))
        if not fields.keys:
            fields.keys = ",".join(self.fields.key_list)      # untouched means kept
        if not fields.models:
            fields.models = list(self.fields.models)          # untouched means kept
        return fields

    def _say(self, text: str) -> None:
        self.query_one("#pf-note", Label).update(text)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "pf-cancel":
            self.dismiss(L("nothing was changed", "ничего не изменено"))
        elif event.button.id == "pf-discover":
            self._start_discovery()
        else:
            self._save()

    def _save(self) -> None:
        fields = self._collect()
        message, refusal = provider_setup.apply(self.ctx.config, self.ctx.agent, fields,
                                                workdir=self.workdir)
        if refusal:
            # The answer stays on the form: a person who typed three fields should
            # read what is wrong without losing what they wrote.
            self._say(refusal)
            return
        self.dismiss(message)

    def _start_discovery(self) -> None:
        url = self.query_one("#pf-url", Input).value.strip()
        keys = self.query_one("#pf-keys", Input).value.strip() or \
            ",".join(self.fields.key_list)
        refusal = provider_setup.url_refusal(url)
        if refusal:
            self._say(refusal)
            return
        self._say(L("asking the endpoint for its models…", "спрашиваю у эндпоинта модели…"))
        self.discover_models(url, keys)

    @work(thread=True, exclusive=True, group="discover")
    def discover_models(self, url: str, keys: str) -> None:
        """Off the drawing thread: a slow endpoint must not freeze the terminal."""
        models, dialect, refusal = provider_setup.discover(url, keys)
        self.call_from_thread(self._models_found, models, dialect, refusal)

    def _models_found(self, models: list[str], dialect: str, refusal: str) -> None:
        if not models:
            self._say(L("nothing found / нечего не найдено — the endpoint gave no list"
                        + (f": {refusal}" if refusal else "")
                        + ". please specify the models yourself",
                        f"нечего не найдено — эндпоинт не назвал ни одной модели"
                        + (f": {refusal}" if refusal else "")
                        + ". укажите модели сами"))
            return
        self.query_one("#pf-models", Input).value = ", ".join(models)
        self._say(L(f"{len(models)} model(s) read — save to keep them",
                    f"{len(models)} модель(ей) получено — сохраните, чтобы оставить"))


def _fallback_text(fields: provider_setup.Fields, name: str) -> str:
    """What `/providers edit` answers where there is no screen to open.

    Outside any interface there is no app to push a form onto and no prompt to
    await. Rather than fail, the command says the four facts and gives the exact
    line that changes them — the same write the form performs, one keystroke away.
    """
    lines = [L("the editor form needs an interface to draw on — run BeeCode normally "
               "or change the fields in one line:",
               "форме нужен интерфейс — запустите BeeCode обычным способом "
               "или измените поля в одну строку:"),
             f"  /key {name or fields.name} <base-url> <key[,key...]> [model ...]"]
    if not fields.url:
        return "\n".join(lines)
    shown = ", ".join(fields.models[:6]) + (" …" if len(fields.models) > 6 else "")
    return "\n".join([
        L("there is no screen to open here, so here is what is configured:",
          "экрана нет — вот что настроено:"),
        f"  url    {fields.url}",
        f"  keys   {provider_setup.key_tails(fields.key_list)}",
        f"  models {shown or '—'}",
        lines[1],
    ])


def open_form(ctx, name: str, adding: bool = False, keys_only: bool = False,
              workdir: str = ".") -> tuple[str, str]:
    """Put the form on the Textual screen. Returns `(message, refusal)`; a refusal
    opens nothing. The classic REPL does not come through here — it awaits
    `guided_form` from `repl.try_guided` instead, because its dialogs cannot be
    started from a synchronous dispatch."""
    from beeagent.ui.editor import running_app

    name = (name or "").strip().lower()
    if not name and not adding:
        return "", L("name the provider to edit: /providers edit <name> — "
                     "/providers lists them, /providers add starts a new one",
                     "назовите провайдера: /providers edit <имя> — список в /providers, "
                     "новый — /providers add")
    fields = provider_setup.current(ctx.config, name) if name else provider_setup.Fields()
    announced = ""
    if adding and name and provider_setup.exists(ctx.config, name):
        # The task's own words: a name that already exists is an EDIT, said out
        # loud, not a duplicate. The form opens filled in and says what it does.
        announced = L(f"{name} already exists — this opens it for editing",
                      f"{name} уже есть — открываю его для правки")
    if keys_only and not name:
        return "", L("name the provider whose keys change: /providers key <name>",
                     "назовите провайдера: /providers key <имя>")
    if keys_only:
        fields.name = fields.name or name       # the name box has nothing to guess
    if not adding and not keys_only and not provider_setup.exists(ctx.config, name):
        if not (fields.url or fields.key_list or fields.models):
            return "", L(f"there is no provider called {name!r} to edit — "
                         f"/providers add {name} creates one",
                         f"провайдера {name!r} нет — /providers add {name} создаст его")

    app = running_app()
    if app is None:
        return _fallback_text(fields, name or fields.name), ""
    prefix = (announced + "\n") if announced else ""
    app.push_screen(ProviderForm(fields, ctx, workdir=workdir, keys_only=keys_only),
                    lambda answer: _answered(app, prefix, answer))
    return (prefix + L("the form is on screen — Tab walks the four fields, Enter saves, "
                       "Esc changes nothing",
                       "форма на экране — Tab по полям, Enter сохранить, "
                       "Esc ничего не меняет")), ""


def _answered(app, prefix: str, answer: str) -> None:
    """Write what the closed form did into the log the user is reading.

    The form answers after the command has returned, so its sentence cannot ride
    on the command's own result. `_note` is how the TUI writes a line it did not
    get from the model, and both of its language slots carry the one string the
    form already chose — the wording was picked by `L()` at the moment of saving.
    """
    note = getattr(app, "_note", None)
    text = (prefix + answer).strip() if answer else ""
    if callable(note):
        if text:
            note("🔑", text, text)
        else:
            note("🔑", "nothing was changed", "ничего не изменено")


# ------------------------------------------------------- classic REPL interview --

class _Cancelled(Exception):
    """Ctrl+C or Ctrl+D anywhere in the interview leaves the config untouched."""


async def _ask(title: str, default: str = "", password: bool = False) -> str:
    """One prompt_toolkit line, awaited on the REPL's own loop.

    Kept this small on purpose: the tests script an interview by replacing this
    function, and a command that drives the real dialogs still runs it.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.formatted_text import HTML

    session: PromptSession = PromptSession()
    try:
        return (await session.prompt_async(HTML(f"<b><ansigreen>🐝 {title}</ansigreen></b> "),
                                           default=default, is_password=password)).strip()
    except (KeyboardInterrupt, EOFError):
        raise _Cancelled from None


async def _yes_no(title: str) -> bool:
    from prompt_toolkit.shortcuts import yes_no_dialog

    try:
        return bool(await yes_no_dialog(title=title,
                                       yes_text=L("Replace", "Заменить"),
                                       no_text=L("Keep and add", "Дополнить"),
                                       ).run_async())
    except (KeyboardInterrupt, EOFError):
        raise _Cancelled from None


def _say(text: str) -> None:
    from rich.text import Text

    console.print(Text.assemble("  🔑 ", Text(text, style="dim")))


async def _ask_pool(existing: list[str], replace_hint: str = "") -> str:
    """Keys, one per line, in the typed order; a blank line finishes the pool.

    Existing keys are kept unless the answer to the replace question says
    otherwise — and whatever happens, only tails ever come back to the screen.
    """
    pool = list(existing)
    if existing:
        replace = await _yes_no(L(f"{len(existing)} key(s) stored ({provider_setup.key_tails(existing)}). "
                                  "Replace the pool, or add to it?",
                                  f"ключей сохранено: {len(existing)} ({provider_setup.key_tails(existing)}). "
                                  "Заменить пул или дополнить?"))
        if replace:
            pool = []
    if replace_hint:
        _say(replace_hint)
    while True:
        key = await _ask(L("key (Enter when done)", "ключ (Enter когда закончишь)"),
                         password=True)
        if not key:
            return ",".join(pool)
        if key not in pool:
            pool.append(key)
        _say(L(f"kept {provider_setup.mask_key(key)} — {len(pool)} in the pool",
               f"записан {provider_setup.mask_key(key)} — в пуле {len(pool)}"))


async def guided_form(ctx, name: str = "", adding: bool = False,
                      keys_only: bool = False, workdir: str = "."):
    """The whole interview for `/providers add|edit|key`, ending in a save.

    Returns a `CommandResult`; `repl.try_guided` calls this on the REPL's loop
    and the caller never sees the prompt_toolkit plumbing.
    """
    from beeagent.ui.commands import CommandResult
    from rich.text import Text

    name = (name or "").strip().lower()
    notes: list[str] = []
    try:
        fields = provider_setup.current(ctx.config, name) if name \
            else provider_setup.Fields()
        if not name and adding:
            while True:
                typed = await _ask(L("name of the endpoint (one word; Enter cancels to /providers)",
                                     "имя эндпоинта (одно слово; Enter отменит к /providers)"))
                refusal = provider_setup.name_refusal(typed)
                if not refusal:
                    name = typed.lower()
                    break
                _say(refusal)
            fields = provider_setup.current(ctx.config, name)
            if provider_setup.exists(ctx.config, name):
                # A name that already exists is an EDIT, said out loud.
                notes.append(L(f"{name} already exists — this edits it, nothing is "
                               f"duplicated", f"{name} уже есть — правим его, "
                               f"дубликатов не будет"))
                _say(notes[-1])
            fields.name, fields.was = name, name
        else:
            fields.name, fields.was = name, name

        if keys_only:
            pool = await _ask_pool(provider_setup.stored_keys(ctx.config, name))
            fields.keys = pool
            fields.url = fields.url  # kept as stored; apply keeps preset/class facts
        else:
            from beeagent.providers.presets import BY_NAME
            preset = BY_NAME.get(name)
            url = fields.url
            if preset is not None:
                # A built-in endpoint keeps its own address — provider_setup
                # refuses to shadow it — so there is nothing to ask here.
                url = preset.url
            else:
                while True:
                    typed = await _ask(L("base url (the part before /chat/completions)",
                                         "base url (часть до /chat/completions)"),
                                       default=url)
                    if not typed and url:
                        typed = url              # Enter on a filled box keeps it
                    refusal = provider_setup.url_refusal(typed)
                    if refusal:
                        _say(refusal)
                        continue
                    url = typed
                    break
            fields.url = url
            fields.keys = await _ask_pool(provider_setup.stored_keys(ctx.config, name))
            models_text = await _ask(L(f"models, comma separated (Enter keeps: "
                                       f"{', '.join(fields.models[:4]) or 'none'})",
                                       f"модели через запятую (Enter оставит: "
                                       f"{', '.join(fields.models[:4]) or 'нет'})"),
                                     default=", ".join(fields.models))
            fields.models = provider_setup.split_models(models_text)
            if not fields.models:
                notes.append(await _discover_into(fields))
        message, refusal = provider_setup.apply(ctx.config, ctx.agent, fields,
                                                workdir=workdir)
    except _Cancelled:
        return CommandResult(output=Text(L("🔑 cancelled — nothing was changed",
                                           "🔑 отменено — ничего не изменено"),
                                         style="dim"))
    if refusal:
        return CommandResult(output=Text("\n".join(notes + [refusal]) if notes else refusal,
                                         style="bold red"))
    return CommandResult(output=Text("\n".join(notes + [message]) if notes else message,
                                     style="bold green"))


async def _discover_into(fields) -> str:
    """Empty models box: ask the endpoint, say what came back, keep it if it did."""
    refusal = provider_setup.url_refusal(fields.url)
    if refusal or not fields.key_list:
        return L("no models given and nothing to ask them from (an address and a key "
                 "are needed) — name them later with /providers models "
                 + fields.name + " <a,b,c>",
                 "моделей нет и спросить негде (нужен адрес и ключ) — назовите их позже: "
                 "/providers models " + fields.name + " <a,b,c>")
    _say(L("asking the endpoint for its models…", "спрашиваю у эндпоинта модели…"))
    models, _dialect, why = await asyncio.to_thread(provider_setup.discover,
                                                    fields.url, fields.keys)
    if not models:
        return L("nothing found / нечего не найдено — the endpoint gave no model list"
                 + (f": {why}" if why else "") + ". name the models yourself: "
                 f"/providers models {fields.name} <a,b,c>",
                 "нечего не найдено — эндпоинт не назвал ни одной модели"
                 + (f": {why}" if why else "") + ". назовите модели сами: "
                 f"/providers models {fields.name} <a,b,c>")
    fields.models = models
    return L(f"{len(models)} model(s) read from the endpoint: "
             f"{', '.join(models[:6])}{' …' if len(models) > 6 else ''}",
             f"{len(models)} модель(ей) получено от эндпоинта: "
             f"{', '.join(models[:6])}{' …' if len(models) > 6 else ''}")
