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
from textual.widgets import (  # noqa: I001
    Button, Footer, Header, Input, Label, ListView, ListItem, RichLog, Static,
)

from beeagent.core.session import Session
from beeagent.i18n import L
from beeagent.ui.bee import BEE_FRAME_COUNT, render_bee
from beeagent.ui.commands import COMMANDS, ReplContext, dispatch, history_body, visible_commands
from beeagent.ui.components import DARK_LEAF, HONEY, LEAF, bee_title, hud_lines

# The picker is the classic dialog (`repl.BEE_DIALOG_STYLE`) drawn in Textual:
# deep hive backdrop, honey headings, leaf frame. A default-styled Textual
# dialog is blue, which is not this program's colour anywhere else.
HIVE_BACKDROP = "#08170a"
HIVE_PANEL = "#0d2410"
HIVE_ROW = "#c8e6c9"
HIVE_CURSOR = "#2e7d32"


def _num(value) -> str:
    """Seconds the way a person reads them: `15`, not `15.0`."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


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
    #hud { color: $text-muted; height: auto; display: none; }
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
        # Whether the line in the stream area is still the waiting one. The frame
        # clock redraws it so a skin that claimed `spinner` can animate it, and it
        # stops mattering the moment the model's words, a thought or a timeout note
        # takes that space over.
        self._waiting_line = False
        # The last text laid on the status label: the frame clock offers a new one
        # twelve times a second, and a label told the same thing needs no repaint.
        self._status_shown = ""
        # The answer the skin was last asked to lay out. Laying out a whole answer
        # is the expensive part of streaming, so it happens once per frame and only
        # over text that changed.
        self._answer_painted = ""
        # Which frame of the layout rhythm this is: see `ANSWER_BEATS`.
        self._answer_beats = 0
        # What the skin last answered for the logo and the panel border, so the
        # clock only touches the real widgets when the picture actually changed.
        self._logo_shown = ""
        self._frame_shown = ""
        # The model's own bytes for the turn in flight. A repaired or a cut-off
        # call is only an honest note if the user can see what was actually sent,
        # and `_stream_buf` is cleared the moment a tool starts.
        self._sent_buf = ""
        # What happened to this turn that the user did not ask for, counted so the
        # status line cannot report a clean turn that was not clean.
        self._turn_flags: dict[str, int] = {}
        # Event names no branch handles, reported once instead of forever.
        self._seen_unknown: set[str] = set()
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
                yield Label("", id="hud")
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
        self._skin_timer = None
        self.set_interval(0.28, self._animate_bee)
        self._fit_to_width()
        self._welcome()
        self._update_status()
        self.prompt.focus()

    def on_resize(self, event) -> None:
        self._fit_to_width()
        self._paint_hud()                # a grid sized for the old window is wrong

    # -- the skin's own clock ----------------------------------------------

    FRAME_SECONDS = 1.0 / 12.0
    #: Every Nth frame the skin owns, the answer area is laid out again. Laying a
    #: block out is Rich's most expensive call on screen — measured on this machine
    #: at ~2 ms mean and 8 ms worst for the pass, so twelve of them a second is a
    #: fifth of a core spent on colour motion nobody can follow while reading. The
    #: HUD, the spinner and the status line keep the 12 fps clock; the answer
    #: re-typesets about three times a second, which is how fast it changes anyway.
    ANSWER_BEATS = 4

    def _sync_skin_clock(self) -> None:
        """Run a 12 fps clock only while the skin on screen has work for one."""
        from beeagent.core import skins

        try:
            wanted = bool(skins.needs_tick())
        except Exception:
            wanted = False
        timer = getattr(self, "_skin_timer", None)
        if wanted and timer is None:
            self._skin_timer = self.set_interval(self.FRAME_SECONDS, self._skin_tick)
        elif not wanted and timer is not None:
            self._stop_skin_clock()

    def _skin_tick(self) -> None:
        """One frame: the skin advances, and the lines it owns redraw."""
        from beeagent.core import skins

        if not self._chrome_ready():
            # The home screen is under a dialog or on its way out: the clock has
            # nothing to draw on, so it stops instead of missing a widget twelve
            # times a second. `_sync_skin_clock` starts it again when the chrome
            # is back, which every command and every finished turn calls for.
            self._stop_skin_clock()
            return
        strip = self._strip_painter()
        try:
            # The skin's `on_frame` gets the same rows its HUD gets: an animation
            # that has nowhere to be seen is the reason "I installed a skin and
            # nothing changed" was ever a true sentence about this program.
            skins.frame(self.FRAME_SECONDS, strip)
        except Exception:
            pass                        # the host does not owe a skin a stack trace
        self._paint_hud(strip)
        self._paint_logo()
        self._paint_frame()
        self._update_status()
        if self._waiting_line:
            self._repaint_waiting()
        self._answer_beats = (self._answer_beats + 1) % self.ANSWER_BEATS
        if self._stream_buf and not self._answer_beats:
            self._repaint_stream()

    def _strip_painter(self):
        """The rows the skin is allowed to paint: its HUD strip, one row at minimum."""
        from beeagent.core import skins
        from beeagent.core.renderer import Painter

        rows = 1
        try:
            rows = max(1, int(skins.surfaces()["hud_rows"] or 1))
        except Exception:
            rows = 1
        return Painter(size=(max(20, self.screen.size.width - 8), rows))

    def _chrome_ready(self) -> bool:
        """Are the home screen's own lines there to be drawn on?"""
        try:
            self.home.query_one("#hud", Label)
        except Exception:
            return False
        return True

    def _stop_skin_clock(self) -> None:
        timer = getattr(self, "_skin_timer", None)
        if timer is not None:
            timer.stop()
            self._skin_timer = None

    def _repaint_waiting(self) -> None:
        """The waiting line, redrawn at the skin's rate — only when a skin animates it.

        Without a claim this is where a 12 fps clock would shuffle the built-in
        joke under the user, because `pending_text()` picks one at random.
        """
        from beeagent.core import skins
        from beeagent.ui.components import markup_text, pending_text

        if not skins.owns("spinner"):
            return
        self.stream.update(markup_text(f"💬 {pending_text()}", "dim italic"))

    def _write_answer(self, text: str) -> None:
        """The finished answer — the skin's block when it holds that surface.

        Their outline replaces ours entirely, title and borders included: two
        frames around one answer is what a half-wired door looks like.
        """
        from beeagent.core import skins

        try:
            mine = skins.answer_render(text, True)
        except Exception:
            mine = None
        self._answer_painted = ""
        if mine is not None:
            self.chatlog.write(mine)
            return
        self.chatlog.write(Text("🐝 BeeCode", style="bold green"))
        self.chatlog.write(Markdown(text))

    def _repaint_stream(self) -> None:
        """The answer area while it grows: redrawn on the clock, never per token.

        `stream_delta` writes the raw buffer as each piece lands, which is the
        cheap line; laying out a whole answer is not, so this pass runs at most
        twelve times a second and only when the text changed since the last one.
        """
        from beeagent.core import skins

        if self._stream_buf == self._answer_painted:
            return
        try:
            mine = skins.answer_render(self._stream_buf)
        except Exception:
            mine = None
        if mine is None:
            return              # nobody holds the block; the plain line already says it
        self._answer_painted = self._stream_buf
        self.stream.update(mine)

    def _paint_hud(self, painter=None) -> None:
        from beeagent.core import skins

        if not self._chrome_ready():
            return                      # a strip has nowhere to go; not the skin's fault
        try:
            label = self.home.query_one("#hud", Label)
            owns = skins.owns("hud")
            if painter is None:
                painter = self._strip_painter()
            # A skin that owns the strip is asked to fill it; a skin that only has
            # `on_frame` has already drawn its animation into the same rows.
            filled = skins.hud_frame(painter, self.FRAME_SECONDS) if owns else True
            lines = [line for line in hud_lines(painter, painter.rows)
                     if str(line).strip()] if filled else []
            if not lines:
                # Nothing drawn — including a skin that broke on this frame — means
                # no strip: an empty reserved row would push the answer down for a
                # picture that is not there.
                if label.display:
                    label.display = False
                    label.update("")
                return
            # One Rich Text per row, joined with real line breaks: the cells carry
            # the colours the skin chose, and a plain string here would flatten the
            # picture it spent the frame building.
            from rich.text import Text as Row

            joined = Row()
            for number, row in enumerate(lines):
                if number:
                    joined.append("\n")
                joined.append_text(row)
            label.display = True
            label.update(joined)
        except Exception as exc:                    # noqa: BLE001 - see below
            # A HUD is decoration: it must not eat the answer. But a strip that
            # silently never appears is a bug nobody can see, so the reason is said
            # once, the same way the skin host says everything else about a skin.
            if not getattr(self, "_hud_warned", False):
                self._hud_warned = True
                self._note("⚠", f"the skin's hud strip could not be drawn: {exc}",
                           f"полосу hud скина нарисовать не смогла: {exc}")

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
        for c in visible_commands():
            # `/model` and `/provider` still work when typed; showing them beside
            # `/models` and `/providers` is what made four commands look like four
            # different decisions to make.
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
            self.chatlog.write(Text(body.strip() or L("no reasoning in this answer",
                                                      "размышлений в этом ответе не было"),
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
        self._sent_buf = ""
        self._turn_flags = {}

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
            self.call_from_thread(self._finish_turn)
        except Exception:
            pass

    def _on_agent_event(self, event: str, data: dict) -> None:
        # Only the line the `status` event draws is the waiting line: every other
        # event writes over that space, and the frame clock must stop animating a
        # thought, a tool note or the answer as if it were still the joke.
        self._waiting_line = event == "status"
        try:
            from beeagent.core import skins

            skins.post(event, data)
        except Exception:
            pass          # a decoration must never cost the user their answer
        if event == "stream_delta":
            self._stream_buf += data.get("text", "")
            self._sent_buf += data.get("text", "")
            self.stream.update(Text(self._stream_buf))
        elif event == "status":
            from beeagent.ui.components import markup_text, pending_text
            # A fresh turn: the bytes the model sends now are the ones the notes
            # about this turn's calls will quote.
            self._sent_buf = ""
            self.stream.update(markup_text(f"💬 {pending_text()}", "dim italic"))
        elif event == "waiting":
            # Silence is not death: say the endpoint is being waited on, and how
            # long, or the user closes a working program.
            seconds = _num(data.get("seconds"))
            self.stream.update(Text(
                f"⌛ {L(f'still waiting for the model… {seconds}s', f'всё ещё жду модель… {seconds}с')}",
                style="dim"))
            self._note("⌛",
                       f"the endpoint has said nothing for {seconds}s — still waiting, "
                       f"nothing was lost",
                       f"эндпоинт молчит {seconds}с — жду дальше, ничего не потерялось")
            self._count("waiting")
        elif event == "reasoning_delta":
            # The text was thrown away here: the stream line said "думает..." and
            # never showed what the model actually thought, so the one thing the
            # classic REPL did show was invisible in the default interface.
            self._think_buf += data.get("text", "")
            tail = " ".join(self._think_buf.split() [-18:])
            self.stream.update(Text(f"💭 {tail}", style="#ffcc00"))
        elif event == "stream_reset":
            # The retry writes over the abandoned fragment. Without this branch
            # the two answers glued together on screen ("half an answer… the real
            # answer") while the transcript held only the clean one.
            fragment = self._stream_buf
            self._stream_buf = ""
            self._sent_buf = ""
            self._answer_painted = ""      # the abandoned text is not a diff baseline
            self.stream.update("")
            self._note("🗑",
                       f"the unfinished answer was thrown away, not appended — the next "
                       f"try starts on a clean screen. Gone: {len(fragment)} characters: "
                       f"“{self._snippet(fragment)}”",
                       f"недописанный ответ выброшен, а не дописан — следующая попытка "
                       f"начнётся с чистого экрана. Выброшено {len(fragment)} символов: "
                       f"«{self._snippet(fragment)}»")
            self._count("stream_reset")
        elif event == "response":
            # An economy cache hit returns without streaming and without `done`,
            # and this interface had no branch for it: the answer existed in the
            # session and on the terminal nowhere.
            self._stream_buf = ""
            self.stream.update("")
            self._write_answer(data.get("text", ""))
        elif event == "context_trimmed":
            dropped = data.get("dropped") or 0
            self._note("✂",
                       f"history did not fit the window: {self._trim_detail(dropped)} fell "
                       f"out of the request — the model gets a digest line instead of them "
                       f"and may contradict what they said. /history keeps the full text",
                       f"история не влезла в окно: {self._trim_detail(dropped)} не попали в "
                       f"запрос — вместо них модель видит строку конспекта и может "
                       f"противоречить тому, что в них было. Полный текст — в /history")
            self._count("context_trimmed", int(dropped))
        elif event == "tool_repaired":
            notes = "; ".join(data.get("notes") or [])
            self._note("🩹",
                       f"incomplete tool call repaired before running: {notes}. The model "
                       f"sent “{self._raw_call()}” — the line that runs next is the fixed "
                       f"shape, not these bytes",
                       f"неполный вызов починен до запуска: {notes}. Модель прислала "
                       f"«{self._raw_call()}» — следующая строка это исправленный вызов, "
                       f"а не эти байты")
            self._count("tool_repaired")
        elif event == "tool_dropped":
            notes = "; ".join(data.get("notes") or [])
            self._note("✂️",
                       f"a tool call arrived cut off and NOTHING ran: {notes}. The model "
                       f"sent “{self._raw_call()}”. It has been asked to send it again — "
                       f"what follows is that retry",
                       f"вызов инструмента пришёл обрезанным и НИЧЕГО не запустилось: "
                       f"{notes}. Модель прислала «{self._raw_call()}». Её попросили "
                       f"прислать заново — дальше будет этот повтор")
            self._count("tool_dropped")
        elif event == "tool_renamed":
            self._note("🔧",
                       f"the model called it “{data.get('from', '')}”, which is not the "
                       f"tool's name; the same tool runs as “{data.get('to', '')}” and is "
                       f"written to history under that name",
                       f"модель звала его «{data.get('from', '')}» — такого имени нет; "
                       f"тот же инструмент запустится как «{data.get('to', '')}» и в "
                       f"истории будет это имя")
            self._count("tool_renamed")
        elif event == "tool_unknown":
            self._note("🤔",
                       f"“{data.get('tool', '')}” is not a tool here — nothing ran; the "
                       f"model got the real list back and continues. /tools shows the names",
                       f"«{data.get('tool', '')}» — не инструмент, ничего не запустилось; "
                       f"модель получила настоящий список и продолжает. Имена — в /tools")
            self._count("tool_unknown")
        elif event == "nudged":
            self._note("🐝",
                       f"the model promised a step but sent no tool call, so nothing ran: "
                       f"“{self._snippet(self._sent_buf)}”. It has been told to act — the "
                       f"next answer is the same question re-asked",
                       f"модель пообещала шаг и не вызвала инструмент — ничего не "
                       f"запустилось: «{self._snippet(self._sent_buf)}». Ей сказали "
                       f"действовать — следующий ответ это тот же вопрос заново")
            self._count("nudged")
        elif event == "queued_sent":
            items = data.get("items") or []
            if items:
                self._note("📨", f"{len(items)} queued message(s) went out with this step",
                           f"{len(items)} сообщ. из очереди ушло с этим шагом")
        elif event == "stopped":
            # The worker ends the loop at the next turn; the partial text is
            # dropped rather than passed off as an answer.
            self._stream_buf = ""
            self.stream.update("")
            self._note("🛑",
                       f"stopped by you after {data.get('turn', 0)} step(s) — "
                       "the session and /tasks are intact",
                       f"остановлено тобой после {data.get('turn', 0)} шаг(ов) — "
                       "сессия и /tasks на месте")
        elif event == "retry":
            self._note("🔁", f"the bee is retrying (attempt {data.get('attempt')}/3)",
                       f"пчела повторяет попытку ({data.get('attempt')}/3)")
        elif event == "done":
            text = self._stream_buf
            self._stream_buf = ""
            self.stream.update("")
            if self._think_buf.strip():
                self.chatlog.write(Text(L("💭 what I thought", "💭 как я думал"),
                                        style="bold #ffcc00"))
                self.chatlog.write(Text(self._think_buf.strip(), style="dim italic"))
                self._think_buf = ""
            if text.strip():
                self._write_answer(text)
            else:
                self.chatlog.write(Text(
                    L("  ⚠ empty answer — try again or switch model (/models)",
                      "  ⚠ пустой ответ — попробуй ещё раз или смени модель (/models)"),
                    style="#ffcc00"))
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
            self._note("⚠", f"tool '{data.get('tool')}' failed: {data.get('message')}",
                       f"инструмент '{data.get('tool')}' упал: {data.get('message')}",
                       style="bold red")
            self._count("tool_error")
        elif event == "tool_denied":
            tool = data.get("tool", "")
            self._note("⛔", f"{tool} did not run — you have not allowed it; /allow {tool} does",
                       f"{tool} не запущено — ты его не разрешал; /allow {tool} разрешит",
                       style="bold red")
            self._count("tool_denied")
        elif event == "provider_fallback":
            if data.get("seat"):
                self._note("🐝", "no g4f on this machine — answering through the pool instead",
                           "g4f на этой машине не ставится — отвечаем через пул",
                           style="#ffcc00")
            else:
                self._note("🐝", "no g4f on this machine — take a seat in the pool:"
                                 " /pool enroll",
                           "g4f на этой машине не ставится — возьми место в пуле:"
                           " /pool enroll", style="#ffcc00")
        elif event == "model_switched":
            self._note("🔄", f"this provider has no “{data.get('from')}” — answering "
                             f"with “{data.get('to')}”",
                       f"у этого провайдера нет «{data.get('from')}» — отвечаем "
                       f"«{data.get('to')}»", style="#ffcc00")
        elif event == "economy_hit":
            self._note("💾", "the answer came from the cache, not from the model",
                       "ответ взялся из кэша, а не от модели", style="bold green")
        elif event == "error":
            self._stream_buf = ""
            self.stream.update("")
            self.chatlog.write(Text(f"error: {data.get('message')}", style="bold red"))
        else:
            # An event no branch caught is an event the user would never have
            # heard. Better one honest line than a silent turn.
            if event not in self._seen_unknown:
                self._seen_unknown.add(event)
                self._note("❓", f"unhandled agent event “{event}”: {data}",
                           f"неизвестное событие «{event}»: {data}", style="bold red")

    # --- monitoring --------------------------------------------------------

    def _note(self, icon: str, english: str, russian: str, style: str = "dim") -> None:
        """One line in the chat log, in the language the user reads in.

        The same shape as the classic REPL's `_note`: both languages are written
        at the call site and `L()` picks one, so a note cannot ship in a single
        language — and cannot be forgotten in the other one either.
        """
        self.chatlog.write(Text.assemble(f"  {icon} ", Text(L(english, russian), style=style)))

    def _count(self, kind: str, amount: int = 1) -> None:
        """Remember that something the user did not ask for happened this turn."""
        self._turn_flags[kind] = self._turn_flags.get(kind, 0) + amount

    @staticmethod
    def _snippet(text: str, limit: int = 140) -> str:
        """The model's bytes as one readable line: newlines folded, tail cut."""
        flat = " ".join((text or "").split())
        if not flat:
            return L("(nothing arrived but whitespace)", "(дошли только пробелы)")
        return flat[:limit] + ("…" if len(flat) > limit else "")

    def _raw_call(self) -> str:
        """What the model actually sent for its tool call, from this turn's text."""
        text = self._sent_buf
        at = text.lower().find('"tool"')
        if at < 0:
            return self._snippet(text)
        start = text.rfind("{", 0, at)
        return self._snippet(text[start if start >= 0 else at:])

    def _trim_detail(self, dropped: int) -> str:
        """How much fell out of the request: how many, of how many, how much text.

        The event carries only a count, and `_window` drops from the oldest end,
        so the oldest `n` messages are named here — with their size and roles,
        because "the task stays in view" was never what the user needed to know.
        """
        messages = (self.ctx.session.messages if self.ctx.session is not None else []) or []
        count = max(0, min(int(dropped or 0), len(messages)))
        if not count:
            return L("nothing", "ничего")
        lost = messages[:count]
        chars = sum(len(str(m.content or "")) for m in lost)
        roles: dict[str, int] = {}
        for msg in lost:
            roles[msg.role] = roles.get(msg.role, 0) + 1
        who = ", ".join(f"{role} x{num}" for role, num in sorted(roles.items()))
        return L(f"{count} of {len(messages)} message(s) ({chars} characters: {who})",
                 f"{count} из {len(messages)} сообщ. ({chars} симв.: {who})")

    def _turn_incidents(self) -> list[str]:
        """Every way this turn was not the clean one the user asked for."""
        flags = self._turn_flags
        parts = []
        if flags.get("context_trimmed"):
            parts.append(L(f"{flags['context_trimmed']} msg(s) summarised away",
                           f"{flags['context_trimmed']} сообщ. сжато"))
        if flags.get("tool_repaired"):
            parts.append(L(f"{flags['tool_repaired']} call(s) repaired",
                           f"{flags['tool_repaired']} вызов(ов) починено"))
        if flags.get("tool_dropped"):
            parts.append(L(f"{flags['tool_dropped']} call(s) NOT run",
                           f"{flags['tool_dropped']} вызов(ов) не запущено"))
        if flags.get("tool_renamed"):
            parts.append(L(f"{flags['tool_renamed']} name(s) corrected",
                           f"{flags['tool_renamed']} имени поправлено"))
        if flags.get("tool_unknown"):
            parts.append(L(f"{flags['tool_unknown']} unknown tool(s)",
                           f"{flags['tool_unknown']} неизвестных инструмента(ов)"))
        if flags.get("tool_error"):
            parts.append(L(f"{flags['tool_error']} tool(s) failed",
                           f"{flags['tool_error']} инструмент(ов) упало"))
        if flags.get("tool_denied"):
            parts.append(L(f"{flags['tool_denied']} tool(s) denied",
                           f"{flags['tool_denied']} инструмент(ов) запрещено"))
        if flags.get("stream_reset"):
            parts.append(L(f"{flags['stream_reset']} partial answer(s) thrown away",
                           f"{flags['stream_reset']} неполный(ых) ответ(ов) выброшено"))
        if flags.get("nudged"):
            parts.append(L(f"{flags['nudged']} nudge(s) to act",
                           f"{flags['nudged']} пинка(ов) к действию"))
        if flags.get("waiting"):
            parts.append(L(f"{flags['waiting']} wait(s) on the endpoint",
                           f"{flags['waiting']} раз(а) ждали эндпоинт"))
        return parts

    def _finish_turn(self) -> None:
        """Close the turn in the log too: the status line can be off-screen."""
        self._waiting_line = False
        incidents = self._turn_incidents()
        if incidents:
            self._note("📋", "this turn: " + ", ".join(incidents),
                       "этот ход: " + ", ".join(incidents), style="bold yellow")
        self._update_status()

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

    def _paint_logo(self) -> None:
        """The header word in the skin's colours, on the clock.

        The full-screen interface has no 48x5 box to spend, so a skin that owns
        `banner` gets the line that is always on screen — the word in the corner —
        while the whole logo still goes into the log at startup. Only the first row
        is used, and a skin that hands back what BeeCode drew changes nothing.
        """
        from beeagent.core import skins
        from beeagent.ui.components import BANNER_ROWS, BANNER_WIDTH

        # One row, not the whole logo: the header shows a line, and laying out five
        # to throw four away costs Rich about a millisecond a frame — on a box that
        # is already compiling. The first row of the shipped art travels along as
        # the shape to recolour, so a skin that reuses it keeps the letterforms.
        top = Text(BANNER_ROWS[0]) if BANNER_ROWS else Text("BeeCode")
        try:
            mine = skins.banner_render(size=(BANNER_WIDTH, 1), default=top)
        except Exception:
            return
        if mine is None:
            return
        if isinstance(mine, str):
            mine = Text(mine.split("\n")[0])
        else:
            try:
                mine = mine.split("\n")[0]
            except Exception:
                mine = Text(str(mine).split("\n")[0])
        if str(mine) == self._logo_shown and self._logo_shown:
            return
        self._logo_shown = str(mine)
        try:
            self.home.query_one("#brand", Label).update(mine)
        except Exception:
            pass

    def _paint_frame(self) -> None:
        """The answer panel's border, in the colour the skin chose for it.

        The box stays ours: this only answers for the colour, so `frame=none` in
        the settings still draws no box even under a skin that owns the surface.
        """
        from beeagent.core import skins

        try:
            color = skins.frame_color("answer", "")
        except Exception:
            return
        if color == self._frame_shown:
            return
        self._frame_shown = color
        try:
            log = self.home.query_one("#log")
        except Exception:
            return
        try:
            # A style may carry "bold" beside the colour; Textual's border wants the
            # colour alone, and an unknown token is the skin's mistake, not a reason
            # to leave the panel with no border at all.
            log.styles.border = ("tall", str(color).split()[-1]) if color else None
        except Exception:
            pass

    def _update_status(self) -> None:
        cfg = self.ctx.config
        n = len(self.ctx.session.messages) if self.ctx.session is not None else 0
        incidents = self._turn_incidents()
        tail = (L(" · this turn: ", " · этот ход: ") + ", ".join(incidents)) if incidents else ""
        from beeagent.ui.components import markup_text, status_line
        from rich.markup import escape

        self._sync_skin_clock()
        # Escaped on the way in, markup on the way out: a skin that keeps our
        # wording hands it back inside its own tags, and a model name holding a
        # bracket must survive that trip.
        line = markup_text(status_line(escape(
            f"model {cfg.model} · provider {cfg.provider} · mode {cfg.mode} · msgs {n}"
            + tail
        )))
        if str(line) != self._status_shown:
            self._status_shown = str(line)
            self.home.query_one("#status", Label).update(line)

    def _welcome(self) -> None:
        from beeagent.core import skins
        from beeagent.ui.components import BANNER_ROWS, BANNER_WIDTH, GRADIENT

        shipped = Text()
        last = len(BANNER_ROWS) - 1
        for i, row in enumerate(BANNER_ROWS):
            shipped.append(row, style=f"bold {GRADIENT[i % len(GRADIENT)]}")
            if i != last:
                shipped.append("\n")
        try:
            mine = skins.banner_render(size=(BANNER_WIDTH, len(BANNER_ROWS)),
                                       default=shipped)
        except Exception:
            mine = None
        self.chatlog.write(mine if mine is not None else shipped)
        notes = getattr(getattr(self.ctx, "agent", None), "startup_notes", "")
        if notes:
            # The classic REPL says this before the first prompt; the log is the
            # only place this interface can say it before the first answer fails.
            self.chatlog.write(Text(notes, style="bold yellow"))
        self.chatlog.write(Text("Free AI coding agent powered by g4f", style="dim"))
        self.chatlog.write(Text(
            "Type a request and press Enter. Type / to see commands. "
            "Click a command in the sidebar to run it; a command with a list to "
            "choose from opens a picker.", style="dim"
        ))
        self.chatlog.write(Text(""))


def run_tui(config=None, session=None):
    BeeCodeApp(config=config, session=session).run()
