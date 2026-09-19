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
"""
from __future__ import annotations

from rich.markdown import Markdown
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import (
    Button, Footer, Header, Input, Label, ListView, ListItem, RichLog, Static,
)

from beeagent.core.session import Session
from beeagent.ui.bee import BEE_FRAME_COUNT, render_bee
from beeagent.ui.commands import COMMANDS, ReplContext, dispatch


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
        self._bee_i = 0
        self._applied_theme = None

    # --- widget shortcuts -------------------------------------------------
    @property
    def chatlog(self) -> RichLog:
        return self.query_one("#log", RichLog)

    @property
    def stream(self) -> Static:
        return self.query_one("#stream", Static)

    @property
    def prompt(self) -> Input:
        return self.query_one("#prompt", Input)

    # --- layout -----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal(id="beebar"):
            yield Static(render_bee(0), id="bee")
            with Vertical(id="statuscol"):
                yield Label("BeeCode", id="brand")
                yield Label("", id="status")
                yield Label("type / for commands · click a command · ctrl+b toggles sidebar", id="hint")
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
        try:
            self.theme = self.ctx.theme
            self._applied_theme = self.ctx.theme
        except Exception:
            pass
        self._populate_commands()
        self.set_interval(0.28, self._animate_bee)
        self._welcome()
        self._update_status()
        self.prompt.focus()

    # --- sidebar ----------------------------------------------------------
    def _populate_commands(self, prefix: str = "") -> None:
        lv = self.query_one("#cmdlist", ListView)
        lv.clear()
        q = prefix.lower()
        for c in COMMANDS:
            if c.name.startswith(q):
                label = Label(f"[bold cyan]/{c.name}[/]  [dim]{c.description}[/]")
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
        name = getattr(event.item, "_cmd_name", "")
        if not name:
            return
        cmd = next((c for c in COMMANDS if c.name == name), None)
        self.prompt.value = "/" + name + (" " if cmd and cmd.arg else "")
        self.prompt.focus()
        self.prompt.cursor_position = len(self.prompt.value)

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
        self.query_one("#sidebar").toggle_class("hidden")

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
        self.chatlog.write(Text(f"{text}", style="bold green"))
        res = dispatch(self.ctx, text)
        if res.action == "clear":
            self.action_clear_log()
        elif res.output is not None:
            self.chatlog.write(res.output)
        if res.action == "quit":
            self.exit()
            return
        self._apply_theme()
        self._update_status()

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

        def cb(ev: str, data: dict):
            try:
                self.call_from_thread(self._on_agent_event, ev, data)
            except Exception:
                pass

        try:
            self.agent.run_sync(text, session=self.ctx.session, callback=cb)
        except Exception as e:
            self.call_from_thread(self.chatlog.write, Text(f"agent error: {e}", style="bold red"))
        finally:
            self.call_from_thread(self._update_status)

    def _on_agent_event(self, event: str, data: dict) -> None:
        if event == "stream_delta":
            self._stream_buf += data.get("text", "")
            self.stream.update(Text(self._stream_buf))
        elif event == "status":
            from beeagent.ui.components import pending_text
            self.stream.update(Text(f"💬 {pending_text()}", style="dim italic"))
        elif event == "reasoning_delta":
            self.stream.update(Text("💭 думает...", style="#ffcc00"))
        elif event == "queued_sent":
            items = data.get("items") or []
            if items:
                self.chatlog.write(Text(f"  📨 доставлено из очереди: {len(items)}", style="dim"))
        elif event == "retry":
            self.chatlog.write(Text(f"  🔁 повтор попытки ({data.get('attempt')}/3)", style="dim"))
        elif event == "done":
            text = self._stream_buf
            self._stream_buf = ""
            self.stream.update("")
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
        elif event == "tool_error":
            self.chatlog.write(Text(f"  ⚠ {data.get('tool')}: {data.get('message')}", style="bold red"))
        elif event == "economy_hit":
            self.chatlog.write(Text("  💾 cache hit", style="bold green"))
        elif event == "error":
            self._stream_buf = ""
            self.stream.update("")
            self.chatlog.write(Text(f"error: {data.get('message')}", style="bold red"))

    # --- chrome -----------------------------------------------------------
    def _animate_bee(self) -> None:
        try:
            bee = self.query_one("#bee", Static)
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
        self.query_one("#status", Label).update(
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
            "Click a command in the sidebar to insert it.", style="dim"
        ))
        self.chatlog.write(Text(""))


def run_tui(config=None, session=None):
    BeeCodeApp(config=config, session=session).run()
