"""The model's own highlighter: `/b/`, `/i/`, `/u/`, `/с.color/`.

A model that wants a word to stand out has two honest options today: Markdown
(`**bold**`, `*italic*`) and nothing else. Underline and colour do not exist
in the Markdown that draws the answers, so this module adds four slash tags
the model can emit anywhere in prose:

    /b bold/                 bold (same as `**`)
    /i italic/               italic (same as `*`)
    /u underline/            underline
    /с.red highlighted/      highlight: red background (Latin `c` works too)

Bold and italic are rewritten to their Markdown spelling and ride the normal
render path. Underline and highlight have no Markdown spelling, so they travel
as private-use markers through the Markdown render and come out as real Rich
spans on the other side (`Answer` below) — the look of everything else does
not move a pixel.

What never parses, on purpose: anything inside fenced code blocks or inline
`` `code` `` (a path like `C:/dir` is data), an unclosed tag, an unknown
colour, and a `/` glued to a word (`and/or`, `a/b test`, `/bin/sh` stay text).
"""
import re

from rich.console import Console, ConsoleOptions, RenderResult
from rich.text import Text

TAG_LETTERS = "biu"
HIGHLIGHT_LETTERS = ("с", "c")          # Cyrillic Es and Latin C — both spell it

COLORS = ("red", "green", "yellow", "blue", "magenta", "cyan", "white", "black")
_HEX = re.compile(r"#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$")

# Marker alphabet, all private-use: Markdown carries them through as plain
# text, and no human keyboard types them by accident.
_M_U_OPEN = "\ue000"
_M_END = "\ue001"
_M_HL_OPEN = "\ue002"
_M_HL_COLOR_END = "\ue003"

_WORD = re.compile(r"[A-Za-z0-9_]")


def _valid_color(value: str) -> str | None:
    text = (value or "").strip().lower()
    if text in COLORS or _HEX.fullmatch(text):
        return text
    return None


def _highlight_style(color: str) -> str:
    """Background `color`, with a foreground that reads on it."""
    dark_on_bright = {"yellow", "white", "cyan", "green"}
    if color in dark_on_bright:
        return f"black on {color}"
    if color.startswith("#"):
        try:
            body = color[1:]
            if len(body) == 3:
                body = "".join(ch * 2 for ch in body)
            r, g, b = (int(body[i:i + 2], 16) for i in (0, 2, 4))
            luminance = (0.299 * r + 0.587 * g + 0.114 * b) / 255
            if luminance > 0.6:
                return f"black on {color}"
        except ValueError:
            pass
    return f"on {color}"


def stylize_prose(text: str) -> str:
    """Rewrite slash tags to Markdown spelling or marker spans.

    Fenced code blocks and inline code pass through untouched; everything else
    is scanned for `/b /`, `/i /`, `/u /` and `/с.color /`.
    """
    parts = re.split(r"(```.*?```|~~~.*?~~~)", text, flags=re.S)
    return "".join(_stylize_chunk(p) if i % 2 == 0 else p
                   for i, p in enumerate(parts))


def _stylize_chunk(chunk: str) -> str:
    out: list[str] = []
    i, n = 0, len(chunk)
    while i < n:
        ch = chunk[i]
        if ch == "\\" and i + 1 < n and chunk[i + 1] in ("`", "/"):
            out.append(chunk[i + 1])
            i += 2
            continue
        if ch == "`":
            end = chunk.find("`", i + 1)
            if end == -1:
                out.append(chunk[i:])
                break
            out.append(chunk[i:end + 1])
            i = end + 1
            continue
        if ch == "/" and (i == 0 or not _WORD.match(chunk[i - 1])):
            replaced = _try_tag(chunk, i)
            if replaced is not None:
                rendered, i = replaced
                out.append(rendered)
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _try_tag(chunk: str, i: int) -> tuple[str, int] | None:
    """A slash tag at `i`, or None. Returns (replacement, next index)."""
    n = len(chunk)
    if i + 1 >= n:
        return None
    letter = chunk[i + 1]
    low = letter.lower()
    rest = i + 2
    if low in TAG_LETTERS:
        if rest >= n or chunk[rest] not in (" ", "\t", "\n"):
            return None
        return _closing(chunk, rest + 1, closer=_md_close(low))
    if letter in HIGHLIGHT_LETTERS:
        if rest >= n or chunk[rest] != ".":
            return None
        match = re.match(r"([A-Za-z#][A-Za-z0-9#]*)\s", chunk[rest + 1:])
        if not match:
            return None
        color = _valid_color(match.group(1))
        if color is None:
            return None
        return _closing(chunk, rest + 1 + match.end(),
                        closer=lambda body: (
                            f"{_M_HL_OPEN}{color}{_M_HL_COLOR_END}"
                            f"{body}{_M_END}"))
    return None


def _md_close(low: str):
    if low == "b":
        return lambda body: f"**{body}**"
    if low == "i":
        return lambda body: f"*{body}*"
    # Underline has no Markdown spelling: a marker span, resolved post-render.
    return lambda body: f"{_M_U_OPEN}{body}{_M_END}"


def _closing(chunk: str, start: int, closer) -> tuple[str, int] | None:
    """The body up to the next bare `/`, or None when it never comes."""
    n = len(chunk)
    j = start
    while j < n:
        if chunk[j] == "\\" and j + 1 < n and chunk[j + 1] == "/":
            j += 2
            continue
        if chunk[j] == "/":
            nxt = chunk[j + 1] if j + 1 < n else ""
            if nxt and _WORD.match(nxt):
                j += 1
                continue
            body = chunk[start:j]
            if not body.strip():
                return None
            return closer(body), j + 1
        j += 1
    return None


def _apply_markers(text: Text) -> Text:
    """Turn marker spans into Rich styles; strip the markers themselves."""
    plain = text.plain
    spans: list[tuple[int, int, str]] = []
    cleaned: list[str] = []
    pos = 0
    pattern = re.compile(
        f"{re.escape(_M_U_OPEN)}(.*?){re.escape(_M_END)}"
        f"|{re.escape(_M_HL_OPEN)}(.*?){re.escape(_M_HL_COLOR_END)}"
        f"(.*?){re.escape(_M_END)}", re.S)
    for match in pattern.finditer(plain):
        cleaned.append(plain[pos:match.start()])
        start = len("".join(cleaned))
        if match.group(1) is not None:
            body = match.group(1)
            cleaned.append(body)
            spans.append((start, start + len(body), "underline"))
        else:
            color, body = match.group(2), match.group(3)
            cleaned.append(body)
            spans.append((start, start + len(body), _highlight_style(color)))
        pos = match.end()
    cleaned.append(plain[pos:])
    out = Text("".join(cleaned))
    for start, end, style in spans:
        out.stylize(style, start, end)
    return out


def _box():
    """Box-drawing that survives the terminal: rounded where UTF-8 lives,
    ASCII where a cp1251 conhost would print `?` instead of corners."""
    import sys

    from rich import box

    encoding = (getattr(sys.stdout, "encoding", "") or "").lower()
    return box.ROUNDED if "utf" in encoding else box.ASCII


def build_table(headers: list[str], rows: list[list[str]], title: str = ""):
    """A Rich table both interfaces draw: REPL prints it, the TUI writes it."""
    from rich.table import Table

    table = Table(box=_box(), show_header=True, title=title or None,
                  header_style="bold")
    for head in headers:
        table.add_column(str(head), overflow="fold")
    for row in rows:
        table.add_row(*[str(cell) for cell in row])
    return table


def table_from_metadata(metadata: dict):
    """The `render` payload `table` travels with, back into a table."""
    if not isinstance(metadata, dict) or metadata.get("render") != "table":
        return None
    headers = [str(h) for h in metadata.get("headers", [])]
    rows = [[str(c) for c in r] for r in metadata.get("rows", [])]
    if not headers:
        return None
    table = build_table(headers, rows)
    dropped = metadata.get("dropped") or 0
    return table, dropped


class Answer:
    """An answer renderable: Markdown look, plus the model's slash tags.

    Renders exactly like `rich.markdown.Markdown` (same parser, same widths —
    the console's own options flow in at render time), then resolves the
    underline/highlight markers the preprocessor left into real spans.
    """

    def __init__(self, text: str):
        self.text = text or ""

    def __rich_console__(self, console: Console, options: ConsoleOptions) -> RenderResult:
        from rich.markdown import Markdown as _Markdown

        md = _Markdown(stylize_prose(self.text), hyperlinks=False)
        assembled = Text()
        for index, line in enumerate(console.render_lines(md, options)):
            if index:
                assembled.append("\n")
            for segment in line:
                assembled.append(segment.text, style=segment.style)
        yield _apply_markers(assembled)
