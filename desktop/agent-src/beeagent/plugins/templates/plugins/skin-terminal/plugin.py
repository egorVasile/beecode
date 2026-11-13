"""A whole skin in one file: every slot gets its own variant.

Frames of ASCII, a header that is just a line of text, a spinner made of blocks,
and an answer renderer that prefixes each line — the last one is a subclass of
the built-in stream, which is what "replaceable renderer" means in practice.
"""
from rich import box
from rich.text import Text


def _ascii_frame():
    # rich's own ASCII box, recolored: a skin may reuse a built-in drawing and
    # change only how it looks.
    return {"box": box.ASCII, "border_style": "white"}


def _header() -> None:
    from beeagent.ui.components import console

    console.print(Text("BeeCode — terminal skin", style="bold"))
    console.print()


def _bar() -> str:
    return "▓▒░ working"


class CompactStream:
    """A renderer that says less: no status line, answers prefixed with '> '."""

    def __init__(self):
        from beeagent.ui.components import ResponseStream

        self._inner = ResponseStream()
        self._prefix = False

    # Everything the REPL calls, forwarded — except the status line, which this
    # skin does not want.
    def on_status(self):
        self._inner._begin_turn()
        self._prefix = True

    def on_thinking(self, text):
        self._inner.on_thinking(text)

    def on_content(self, text):
        self._inner.on_content(("> " + text) if self._prefix else text)
        self._prefix = False

    def on_tool_start(self, data=None):
        self._inner.on_tool_start()

    def on_done(self):
        self._inner.on_done()

    def on_error(self, msg):
        self._inner.on_error(msg)

    def on_response(self, text):
        self._inner.on_response(text)

    def on_reset(self):
        self._inner.on_reset()

    def on_tool_end(self, *args, **kwargs):
        pass

    def reset(self):
        self._inner.reset()

    def peek(self):
        self._inner.peek()

    def thinking(self):
        return self._inner.thinking()


def setup(api) -> None:
    api.skin("frame", "ascii-term", _ascii_frame())
    api.skin("banner", "header-line", _header)
    api.skin("spinner", "bars", _bar)
    api.skin("stream", "compact", CompactStream)
    api.set_skin("frame", "ascii-term")
    api.set_skin("banner", "header-line")
    api.set_skin("spinner", "bars")
    api.set_skin("stream", "compact")
