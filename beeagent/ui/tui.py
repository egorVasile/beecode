"""Full-screen, mouse-driven BeeCode TUI built on Textual.

Layout adapts to the terminal size:
  ┌ header (title + clock) ───────────────────────────────┐
  │ bee bar: animated pixel bee + status line              │
  ├ sidebar (clickable commands) ┬ chat log ───────────────┤
  │ /command list (live filter)  │ streamed agent output   │
  ├ input + Run/Clear/Mode/Quit ─┴─────────────────────────┤
  └ footer (key bindings) ─────────────────────────────────┘

Everything is clickable: command list items, buttons, the input, and the log
scrolls with the mouse wheel. The agent runs in a background worker thread so
the UI stays responsive and streams into the log.

A command that has a list to choose from opens `BeePicker`, the same dialog the
classic REPL puts up: one screen, one selection path, driven by the shared
picker spec table.
"""
from __future__ import annotations

from rich.markdown import Markdown
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, Footer, Header, Input, Label, ListView, ListItem, RichLog, Static,
)

from beeagent.core.session import Session
from beeagent.i18n import L
from beeagent.ui.bee import BEE_FRAME_COUNT, render_bee
from beeagent.ui.commands import COMMANDS, ReplContext, dispatch, history_body
from beeagent.ui.components import DARK_LEAF, HONEY, LEAF, bee_title

# The picker is the classic dialog (`repl.BEE_DIALOG_STYLE`) drawn in Textual:
# deep hive backdrop, honey headings, leaf frame. A default-styled Textual
# dialog is blue, which is not this program's colour anywhere else.
HIVE_BACKDROP = "#08170a"
HIVE_PANEL = "#0d2410"
HIVE_ROW = "#c8e6c9"
HIVE_CURSOR = "#2e7d32"


class BeePicker(ModalScreen):
    """The one choice list every picker command opens.

    The classic REPL answers a bare `/models`, `/mode`, `/theme` with a
    prompt_toolkit dialog; the TUI had no dialog at all, so the same command
    could only print a list and leave the value to be retyped by hand. This
    screen is fed straight from the same spec table (`repl._picker_specs`) and
    hands back the chosen *value* — applying it happens once, in
    `BeeCodeApp._apply_choice`. A future picker is a row in that table, not a
    new widget here.
    """

    CSS = f"""
    /* The dialog is painted on a full-screen layer of its own rather than on the
       screen node: Textual hands the app sheet's `Screen` background to a modal
       screen, so `#bee-picker` gets the theme's grey and never the hive. */
    #picker-back {{
        width: 1fr;
        height: 1fr;
        align: center middle;
        background: {HIVE_BACKDROP};
    }}
    #picker {{
        width: 90%;
        max-width: 110;
        min-width: 40;
        height: auto;
        max-height: 100%;
        padding: 1 2;
        background: {HIVE_PANEL};
        border: heavy {LEAF};
    }}
    #picker-title {{ width: 1fr; height: auto; }}
    #picker-list {{
        height: auto;
        max-height: 14;
        margin-top: 1;
        padding: 0 1;
        background: {HIVE_BACKDROP};
        border: tall {DARK_LEAF};
    }}
    #picker-list > ListItem {{
        height: auto;
        color: {HIVE_ROW};
        background: {HIVE_BACKDROP};
    }}
    #picker-list > ListItem.-highlight {{
        color: {HONEY};
        background: {HIVE_CURSOR};
        text-style: bold;
    }}
    #picker-list:focus > ListItem.-highlight {{
        color: #ffffff;
        background: {HIVE_CURSOR};
        text-style: bold;
    }}
    #picker-buttons {{ width: 1fr; height: auto; align: right middle; margin-top: 1; }}
    #picker-buttons > Button {{
        background: {HIVE_PANEL};
        color: {HIVE_ROW};
        margin-left: 2;
    }}
    #picker-buttons > Button:focus {{
        background: {DARK_LEAF};
        color: {HIVE_BACKDROP};
        text-style: bold;
    }}
    #picker-hint {{ width: 1fr; color: {HIVE_ROW}; margin-top: 1; }}
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, title: str, pairs: list) -> None:
        super().__init__(id="bee-picker")
        self._title = title
        self._pairs = [(str(value), str(label)) for value, label in pairs]

    def compose(self) -> ComposeResult:
        with Vertical(id="picker-back"):
            with Vertical(id="picker"):
                yield Static(bee_title(self._title), id="picker-title")
                yield ListView(*(self._row(value, label) for value, label in self._pairs),
                               id="picker-list")
                with Horizontal(id="picker-buttons"):
                    yield Button(L("Select", "Выбрать"), id="picker-ok")
                    yield Button(L("Cancel", "Отмена"), id="picker-cancel")
                yield Static(L("click it, or ↑↓ + Enter · Esc cancels",
                               "кликни, или ↑↓ + Enter · Esc отменяет"), id="picker-hint")

    @staticmethod
    def _row(value: str, label: str) -> ListItem:
        # A Text, not markup: a catalog description is user data and `[...]` in
        # it would otherwise be read as a style tag.
        item = ListItem(Label(Text(label)))
        item._value = value
        return item

    def on_mount(self) -> None:
        self.query_one("#picker-list", ListView).focus()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        self.dismiss(getattr(event.item, "_value", None))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "picker-cancel":
            self.dismiss(None)
            return
        item = self.query_one("#picker-list", ListView).highlighted_child
        self.dismiss(getattr(item, "_value", None) if item is not None else None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class BeeCodeApp(App):
    TITLE = "BeeCode"
    SUB_TITLE = "free AI coding agent · g4f"

    CSS = """
    Screen { background: $surface-darken-1; }

    #beebar { height: 10; dock: top; padding: 0 1; background: $boost; }
    #bee { width: 16; height: 9; content-align: center middle; }
    #statuscol { width: 1fr; height: 100%; padding: 1 2; }
    #brand { color: $warning; text-style: bold; }
    #status { color: $text-muted; }
    #hint { color: $text-disabled; }

    #body { height: 1fr; }
    #sidebar { width: 40; dock: left; border-right: tall $primary; background: $surface; }
    #sidebar.hidden { display: none; }
    .side-title { color: $primary; text-style: bold; padding: 1 1 0 1; }
    #cmdlist { height: 1fr; }

    #chat { width: 1fr; }
    #log { height: 1fr; border: tall $secondary; background: $surface; }
    #stream { height: auto; max-height: 14; padding: 0 1; color: $text; }

    #inputbar { height: 3; dock: bottom; padding: 0 1; align: left middle; }
    #prompt { width: 1fr; }
    #inputbar Button { margin-left: 1; }
    """

    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("f1", "help", "Help"),
    ]

    def __init__(self, config=None, session=None):
        super().__init__()
        from beeagent.config.loader import load_config
        from beeagent.core.agent import Agent

        self.config = config or load_config()
        self.agent = Agent(config=self.config)
        self.ctx = ReplContext(
            agent=self.agent,
            config=self.config,
            session=session or Session(),
        )
        self._stream_buf = ""
        self._think_buf = ""
        self._bee_i = 0
        self._applied_theme = None
        self._sidebar_user_set = False
        # The screen this app composes its own chrome on. `app.query_one` follows
        # the *active* screen, so while a picker dialog is up `#log`, `#sidebar`
        # and `#status` are simply not there -- and a streamed answer, a resize or
        # ctrl+b does not wait for the dialog to close.
        self._home_screen = None

    # --- widget shortcuts -------------------------------------------------
    @property
    def home(self) -> Screen:
        """The app's own screen, dialog or no dialog on top of it."""
        screen = self._home_screen
        return screen if screen is not None and screen.is_attached else self.screen

    @property
    def chatlog(self) -> RichLog:
        return self.home.query_one("#log", RichLog)

    @property
    def stream(self) -> Static:
        return self.home.query_one("#stream", Static)

    @property
    def prompt(self) -> Input:
        return self.home.query_one("#prompt", Input)

    # --- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="beebar"):
            yield Static(render_bee(0), id="bee")
            with Vertical(id="statuscol"):
                yield Label("BeeCode", id="brand")
                yield Label("", id="status")
                yield Label("type / for commands · click runs it · ctrl+b toggles sidebar", id="hint")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Label("COMMANDS", classes="side-title")
                yield ListView(id="cmdlist")
            with Vertical(id="chat"):
                yield RichLog(id="log", markup=True, wrap=True, highlight=True, min_width=20)
                yield Static("", id="stream")
        with Horizontal(id="inputbar"):
            yield Input(placeholder="Ask BeeCode…  ( / for commands )", id="prompt")
            yield Button("Run", id="run", variant="primary")
            yield Button("Clear", id="clear")
            yield Button("Mode", id="mode")
            yield Button("Quit", id="quit", variant="error")
        yield Footer()

    def on_mount(self) -> None:
        self._home_screen = self.screen
        try:
            self.theme = self.ctx.theme
            self._applied_theme = self.ctx.theme
        except Exception:
            pass
        self._populate_commands()
        self.set_interval(0.28, self._animate_bee)
        self._fit_to_width()
        self._welcome()
        self._update_status()
        self.prompt.focus()

    def on_resize(self, event) -> None:
        self._fit_to_width()

    def _fit_to_width(self) -> None:
        """On a phone the 40-column sidebar is the whole screen.

        Hidden until the terminal is wide enough to spare it. A sidebar the user
        toggled by hand is left alone -- auto-hiding on every resize would take
        back a choice they just made.
        """
        if self._sidebar_user_set:
            return
        # `screen.width` is None before the first layout pass, which made every
        # terminal look narrow; `app.size` is the one that is actually filled in.
        width = getattr(self.size, "width", 0) or getattr(self.home, "width", 0) or 80
        sidebar = self.home.query_one("#sidebar")
        if width < 100:
            sidebar.add_class("hidden")
        else:
            sidebar.remove_class("hidden")

    # --- sidebar ----------------------------------------------------------
    def _populate_commands(self, prefix: str = "") -> None:
        lv = self.home.query_one("#cmdlist", ListView)
        lv.clear()
        q = prefix.lower()
        for c in COMMANDS:
            if c.name.startswith(q):
                label = Label(Text.assemble((f"/{c.name}", f"bold {HONEY}"),
                                            (f"  {c.description}", "dim")))
                item = ListItem(label)
                item._cmd_name = c.name
                lv.append(item)

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id != "prompt":
            return
        value = event.value
        if value.startswith("/"):
            self._populate_commands(value.split(" ")[0][1:])
        else:
            self._populate_commands("")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        """A clicked command is a typed one: same routing, same picker.

        It used to stop at pasting the name into the input, so every click needed
        a second Enter and none of the list commands ever offered its dialog.
        """
        name = getattr(event.item, "_cmd_name", "")
        if not name:
            return
        cmd = next((c for c in COMMANDS if c.name == name), None)
        if self._picker_spec(name) is None and self._needs_argument(cmd):
            # A free value (`/read <path>`, `/key <provider> <token>`) still has
            # to be typed; the filter list is the wrong place to invent one.
            self.prompt.value = "/" + name + " "
            self.prompt.focus()
            self.prompt.cursor_position = len(self.prompt.value)
            return
        self.prompt.value = ""
        self._handle_command("/" + name)

    @staticmethod
    def _needs_argument(cmd) -> bool:
        return cmd is not None and bool(cmd.arg or "<" in (cmd.usage or ""))

    # --- pickers ----------------------------------------------------------
    def _picker_spec(self, name: str):
        """Look the command up in the table the classic REPL already uses.

        One source for both interfaces: `/models`, `/mode`, `/theme` and the rest
        cannot drift into "the REPL has a dialog, the TUI prints a list".
        """
        if not name:
            return None
        from beeagent.ui.repl import _picker_specs

        try:
            return _picker_specs(self.ctx).get(name.lower())
        except Exception:
            return None

    def _open_picker(self, name: str) -> bool:
        """Put up the choice list. False when there is nothing to choose from."""
        spec = self._picker_spec(name)
        if spec is None:
            return False
        title, values_fn, apply_cmd, _current_fn = spec
        try:
            raw = values_fn()
        except Exception:
            return False
        pairs = [v if isinstance(v, tuple) else (v, v) for v in (raw or [])]
        if not pairs:
            return False
        self.push_screen(BeePicker(title, pairs),
                         lambda choice: self._apply_choice(apply_cmd, name, choice))
        return True

    def _apply_choice(self, apply_cmd: str, name: str, choice) -> None:
        """The one place a picked value becomes a real change."""
        if choice is None:
            # Cancelled: fall back to the plain text output, exactly like the REPL.
            self._apply_result(dispatch(self.ctx, "/" + name))
            return
        line = f"{apply_cmd} {choice}"
        self.chatlog.write(Text(line, style="bold green"))
        self._apply_result(dispatch(self.ctx, line))

    # --- buttons & keys ---------------------------------------------------
    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "run":
            self._submit()
        elif bid == "clear":
            self.action_clear_log()
        elif bid == "mode":
            new = "economy" if self.ctx.config.mode == "normal" else "normal"
            self._handle_command(f"/mode {new}")
        elif bid == "quit":
            self.exit()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "prompt":
            self._submit()

    def action_quit_app(self) -> None:
        self.exit()

    def action_clear_log(self) -> None:
        self.chatlog.clear()
        self.stream.update("")

    def action_toggle_sidebar(self) -> None:
        self._sidebar_user_set = True
        self.home.query_one("#sidebar").toggle_class("hidden")

    def action_help(self) -> None:
        self._handle_command("/help")

    # --- command / agent --------------------------------------------------
    def _submit(self) -> None:
        text = self.prompt.value.strip()
        if not text:
            return
        self.prompt.value = ""
        if text.startswith("/"):
            self._handle_command(text)
        else:
            self.chatlog.write(Text(f"› {text}", style="bold cyan"))
            self._run_agent(text)

    def _handle_command(self, text: str) -> None:
        """The one entry point: a typed line, a clicked command, a picked value.

        A bare command that has a list to choose from opens the picker instead of
        printing that list and asking for the value again.
        """
        self.chatlog.write(Text(f"{text}", style="bold green"))
        parts = text.split()
        name = parts[0][1:] if parts and parts[0].startswith("/") else ""
        if len(parts) == 1 and self._open_picker(name):
            return
        self._apply_result(dispatch(self.ctx, text))

    def _apply_result(self, res) -> None:
        if res.action == "clear":
            self.action_clear_log()
        elif res.output is not None:
            self.chatlog.write(res.output)
        if res.action == "quit":
            self.exit()
            return
        if res.action == "thinking_pager":
            # The REPL scrolls it in a pager; here the log is the pager.
            from beeagent.ui.components import thinking_body

            body = thinking_body()
            self.chatlog.write(Text(body.strip() or "размышлений в этом ответе не было",
                                    style="dim italic"))
        if res.action == "history_pager":
            # `/history` promised "scroll with the wheel, q closes it" and opened
            # nothing: the whole conversation goes into the log, which is the pager
            # of this interface.
            body = history_body(self.ctx.session).strip()
            self.chatlog.write(Text(body or L("(empty session)", "(сессия пуста)"),
                                    style="dim"))
        if res.action == "stop":
            # `_cmd_stop` already set the loop's flag; the worker holding this
            # interface has to be released too, or an answer that streams forever
            # leaves the TUI busy until ctrl+q.
            self._stop_agent_worker()
            self.stream.update("")
        if res.action == "update":
            self._run_update()
        self._apply_theme()
        self._update_status()

    def _stop_agent_worker(self) -> None:
        try:
            self.workers.cancel_group(self, "agent")
        except Exception:
            pass

    @work(thread=True, exclusive=True, group="update")
    def _run_update(self) -> None:
        """Actually run what `/update` announced. The log is the progress bar.

        `update_self` is a blocking git/pip call that prints: off the UI thread,
        and its stdout captured, or it draws over the app it is updating.
        """
        import io
        from contextlib import redirect_stdout

        from beeagent.cli import update_self

        self.call_from_thread(self.chatlog.write,
                             Text(L("  ⬇️ updating BeeCode… this takes a minute",
                                    "  ⬇️ обновляю BeeCode… это минута"), style="dim"))
        out = io.StringIO()
        try:
            with redirect_stdout(out):
                code = update_self()
        except Exception as e:
            code = -1
            out.write(str(e))
        if code == 0:
            note = L("  ✅ updated — close this window and start `beecode` again",
                     "  ✅ готово — закрой окно и запусти `beecode` заново")
            style = "dim"
        elif code == -1:
            note = L(f"  ❌ the updater itself failed: {out.getvalue().strip()[:160]}",
                     f"  ❌ сам обновлятор упал: {out.getvalue().strip()[:160]}")
            style = "bold red"
        else:
            note = L(f"  ❌ the update did not finish (exit {code}) — /doctor says why",
                     f"  ❌ обновление не дошло (код {code}) — /doctor объяснит почему")
            style = "bold red"
        self.call_from_thread(self.chatlog.write, Text(note, style=style))

    def _apply_theme(self) -> None:
        if self.ctx.theme == self._applied_theme:
            return
        try:
            self.theme = self.ctx.theme
            self._applied_theme = self.ctx.theme
        except Exception as e:
            self.chatlog.write(Text(f"theme '{self.ctx.theme}' unavailable: {e}", style="red"))
            self._applied_theme = self.ctx.theme

    @work(thread=True, exclusive=True, group="agent")
    def _run_agent(self, text: str) -> None:
        self._stream_buf = ""
        self._think_buf = ""

        def cb(ev: str, data: dict):
            try:
                self.call_from_thread(self._on_agent_event, ev, data)
            except Exception:
                pass

        try:
            self.agent.run_sync(text, session=self.ctx.session, callback=cb)
        except Exception as e:
            # After /stop the worker may already be cancelled, and writing through
            # a dead one would replace the answer with a Textual traceback.
            self._to_log(Text(f"agent error: {e}", style="bold red"))
        finally:
            self._stop_writing()

    def _to_log(self, renderable) -> None:
        try:
            self.call_from_thread(self.chatlog.write, renderable)
        except Exception:
            pass

    def _stop_writing(self) -> None:
        try:
            self.call_from_thread(self._update_status)
        except Exception:
            pass

    def _on_agent_event(self, event: str, data: dict) -> None:
        if event == "stream_delta":
            self._stream_buf += data.get("text", "")
            self.stream.update(Text(self._stream_buf))
        elif event == "status":
            from beeagent.ui.components import pending_text
            self.stream.update(Text(f"💬 {pending_text()}", style="dim italic"))
        elif event == "reasoning_delta":
            # The text was thrown away here: the stream line said "думает..." and
            # never showed what the model actually thought, so the one thing the
            # classic REPL did show was invisible in the default interface.
            self._think_buf += data.get("text", "")
            tail = " ".join(self._think_buf.split() [-18:])
            self.stream.update(Text(f"💭 {tail}", style="#ffcc00"))
        elif event == "response":
            # An economy cache hit returns without streaming and without `done`,
            # and this interface had no branch for it: the answer existed in the
            # session and on the terminal nowhere.
            self._stream_buf = ""
            self.stream.update("")
            self.chatlog.write(Text("🐝 BeeCode", style="bold green"))
            self.chatlog.write(Markdown(data.get("text", "")))
        elif event == "queued_sent":
            items = data.get("items") or []
            if items:
                self.chatlog.write(Text(f"  📨 доставлено из очереди: {len(items)}", style="dim"))
        elif event == "stopped":
            # The worker ends the loop at the next turn; the partial text is
            # dropped rather than passed off as an answer.
            self._stream_buf = ""
            self.stream.update("")
            self.chatlog.write(Text(
                f"  🛑 остановлено тобой после {data.get('turn', 0)} шаг(ов) — "
                "сессия и /tasks на месте", style="dim"))
        elif event == "retry":
            self.chatlog.write(Text(f"  🔁 повтор попытки ({data.get('attempt')}/3)", style="dim"))
        elif event == "done":
            text = self._stream_buf
            self._stream_buf = ""
            self.stream.update("")
            if self._think_buf.strip():
                self.chatlog.write(Text("💭 как я думал", style="bold #ffcc00"))
                self.chatlog.write(Text(self._think_buf.strip(), style="dim italic"))
                self._think_buf = ""
            if text.strip():
                self.chatlog.write(Text("🐝 BeeCode", style="bold green"))
                self.chatlog.write(Markdown(text))
            else:
                self.chatlog.write(Text("  ⚠ пустой ответ — попробуй ещё раз или смени модель (/models)", style="#ffcc00"))
        elif event == "tool_start":
            self._stream_buf = ""
            self.stream.update("")
            args = ", ".join(f"{k}={v!r}" for k, v in data.get("args", {}).items())
            if len(args) > 80:
                args = args[:77] + "..."
            self.chatlog.write(Text(f"  ⏳ {data.get('tool')} {args}", style="bold yellow"))
        elif event == "tool_end":
            mark = "❌" if data.get("error") else "✅"
            color = "red" if data.get("error") else "green"
            self.chatlog.write(Text(f"  {mark} {data.get('tool')}", style=f"bold {color}"))
            out = (data.get("output") or "").strip()
            if out and data.get("error"):
                self.chatlog.write(Text("    " + out[:300], style="dim red"))
            elif out and data.get("tool") == "diagram":
                # The model reads the drawing back from the tool result; the user
                # should see the same picture without opening an SVG.
                self.chatlog.write(Text(out, style="#8fbf6f"))
        elif event == "tool_error":
            self.chatlog.write(Text(f"  ⚠ {data.get('tool')}: {data.get('message')}", style="bold red"))
        elif event == "tool_denied":
            tool = data.get("tool", "")
            self.chatlog.write(Text(f"  ⛔ {tool} — заблокировано, разрешить: /allow {tool}",
                                    style="bold red"))
        elif event == "provider_fallback":
            note = ("  🐝 g4f недоступен на этой системе — отвечаем через пул" if data.get("seat")
                    else "  🐝 g4f недоступен — возьми место в пуле: /pool enroll")
            self.chatlog.write(Text(note, style="#ffcc00"))
        elif event == "model_switched":
            self.chatlog.write(Text(f"  🔄 {data.get('from')} → {data.get('to')}", style="#ffcc00"))
        elif event == "economy_hit":
            self.chatlog.write(Text("  💾 cache hit", style="bold green"))
        elif event == "error":
            self._stream_buf = ""
            self.stream.update("")
            self.chatlog.write(Text(f"error: {data.get('message')}", style="bold red"))

    # --- chrome -----------------------------------------------------------
    def _animate_bee(self) -> None:
        try:
            bee = self.home.query_one("#bee", Static)
        except Exception:
            return
        if self.ctx.bee_enabled:
            bee.update(render_bee(self._bee_i))
            self._bee_i = (self._bee_i + 1) % BEE_FRAME_COUNT
        else:
            bee.update(render_bee(0))

    def _update_status(self) -> None:
        cfg = self.ctx.config
        n = len(self.ctx.session.messages) if self.ctx.session is not None else 0
        self.home.query_one("#status", Label).update(
            f"model {cfg.model} · provider {cfg.provider} · mode {cfg.mode} · msgs {n}"
        )

    def _welcome(self) -> None:
        from beeagent.ui.components import BANNER_ROWS, GRADIENT
        banner = Text()
        last = len(BANNER_ROWS) - 1
        for i, row in enumerate(BANNER_ROWS):
            banner.append(row, style=f"bold {GRADIENT[i % len(GRADIENT)]}")
            if i != last:
                banner.append("\n")
        self.chatlog.write(banner)
        self.chatlog.write(Text("Free AI coding agent powered by g4f", style="dim"))
        self.chatlog.write(Text(
            "Type a request and press Enter. Type / to see commands. "
            "Click a command in the sidebar to run it; a command with a list to "
            "choose from opens a picker.", style="dim"
        ))
        self.chatlog.write(Text(""))


def run_tui(config=None, session=None):
    BeeCodeApp(config=config, session=session).run()
