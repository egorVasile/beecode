"""Full-screen, scrollable text viewer (conversation history, model reasoning).

A plain console dump cannot be read: long answers overflow the terminal's
scrollback, and rich's `console.pager()` only understands Enter on Windows
conhost. This viewer owns the screen and scrolls with the mouse wheel, the
arrows and PgUp/PgDn.

The apps must be awaited (`run_async`) when called from the REPL, which is
itself a coroutine: prompt_toolkit's blocking `.run()` spins up a fresh event
loop and asyncio refuses that from a running one.
"""
from __future__ import annotations

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.formatted_text import to_formatted_text
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

from beeagent.i18n import L
from beeagent.ui.components import brand_ramp

def hint() -> str:
    return L(" wheel or arrows - scroll | PgUp/PgDn - page | Home/End - ends | q - close",
             " колесо или стрелки - листать | PgUp/PgDn - страница | Home/End - край | q - закрыть")

# BeeCode hive palette: deep green backdrop, honey title, leaf accents.
VIEW_STYLE = Style.from_dict({
    "background": "bg:#08170a",
    "frame": "bg:#0d2410",
    "frame.border": "#43a047",
    "frame.label": "bold #ffcc00",
    "text": "#eafbe7 bg:#0d2410",
    "scrollbar.background": "bg:#08170a",
    "scrollbar.button": "bg:#43a047",
    "hint": "#7cb342 bg:#08170a",
})


def _page_rows(app) -> int:
    try:
        return max(1, app.output.get_rows() - 8)
    except Exception:
        return 20


def viewer_key_bindings() -> KeyBindings:
    """Scrolling keys. They drive the buffer's cursor; the Window follows it."""
    kb = KeyBindings()

    @kb.add("up", eager=True)
    def _up(event):
        event.buff.cursor_up()

    @kb.add("down", eager=True)
    def _down(event):
        event.buff.cursor_down()

    @kb.add("pageup", eager=True)
    def _pgup(event):
        event.buff.cursor_up(_page_rows(event.app))

    @kb.add("pagedown", eager=True)
    def _pgdn(event):
        event.buff.cursor_down(_page_rows(event.app))

    @kb.add("home", eager=True)
    def _home(event):
        event.buff.cursor_position = 0

    @kb.add("end", eager=True)
    def _end(event):
        event.buff.cursor_position = len(event.buff.text)

    @kb.add("q")
    @kb.add("escape")
    @kb.add("enter")
    @kb.add("c-c")
    def _close(event):
        event.app.exit()

    return kb


def build_app(title: str, body: str) -> Application:
    """Create the full-screen viewer application for `body`."""
    buffer = Buffer(read_only=True, document=Document(body.rstrip("\n") or " ", 0))
    kb = viewer_key_bindings()
    content = Window(
        content=BufferControl(buffer=buffer, key_bindings=kb),
        wrap_lines=True,
        style="class:text",
    )
    layout = Layout(
        HSplit([
            Frame(body=content, title=brand_ramp(f" 🐝 {title} ")),
            Window(
                content=FormattedTextControl(to_formatted_text(hint())),
                height=1,
                style="class:hint",
            ),
        ]),
        focused_element=content,
    )
    return Application(
        layout=layout,
        key_bindings=kb,
        mouse_support=True,
        full_screen=True,
        style=VIEW_STYLE,
    )


async def show_scrolled(title: str, body: str) -> bool:
    """Await the viewer. Returns False when it could not run (piped output)."""
    if not body.strip():
        return False
    try:
        await build_app(title, body).run_async()
        return True
    except KeyboardInterrupt:
        return True          # Ctrl+C closes the viewer, it is not an error
    except Exception:
        return False
