"""`/edit-ui <file>` — the human's own look at a file, inside BeeCode.

Why this is a plugin screen and not a tool
------------------------------------------
The model has an `edit` tool and always will; nothing here wraps, shadows or
replaces it. This module is for the *person* at the terminal: the agent has just
changed something, the user wants to read the result and fix one comma without
leaving the app and without starting an editor BeeCode cannot see. So there is no
tool registered here, only a screen — and the screen is reachable only while
BeeCode is already running inside Textual.

What is deliberately not here
-----------------------------
No syntax highlighting, no LSP, no completion, no regex replace, no multi-buffer,
no splits, no project drawer. Each of those is a different product. This one is
"look at this file and fix a line", and it stays honest about being that: it
loads a window of the file, says exactly how big that window is, and refuses to
write bytes it cannot rebuild exactly.

Colour
------
Textual's default blue dialog is not this program's colour anywhere else, so
every surface here takes the hive palette the picker and the classic dialog use:
deep green backdrop, leaf frames, honey for whatever the user is being asked to
notice. The values are the ones in `BeePicker.CSS` (`beeagent/ui/tui.py`) and
`ui/viewer.py:VIEW_STYLE`; they are repeated rather than imported because this
module must not depend on the app that hosts it.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Label, Static, TextArea

from beeagent.i18n import L
from beeagent.tools.base import read_text_preserving, write_text_preserving
from beeagent.ui.components import DARK_LEAF, HONEY, LEAF, bee_title

# --- the hive palette (see the module docstring for where these came from) ---
HIVE_BACKDROP = "#08170a"
HIVE_PANEL = "#0d2410"
HIVE_ROW = "#c8e6c9"
HIVE_LINE = "#12301a"     # the row the cursor is standing on
HIVE_FIELD = "#0a1d0c"    # the sunken prompt/find field

# --- how much of a file a person can be shown before it stops being a window --
# Handing a document to the widget costs about 25 microseconds per line, so
# 5,000 lines is a tenth of a second and 200,000 is four seconds of load plus
# more than two for every scroll. That second number is the freeze this cap
# exists to prevent: opening a 40 MB log must cost the same as opening a small
# file, because only the cap's worth of bytes is ever read.
MAX_LINES = 5000
MAX_BYTES = 512 * 1024
# How much of a capped file may be copied through untouched by a save. Splicing
# an 8 KB tail back on is a save; splicing a 40 MB one is a memory spike for no
# gain, and the agent's `edit` tool is the right way into that part of the file.
MAX_TAIL_BYTES = 8 * 1024 * 1024

# `str.splitlines()` — and with it Textual's own document — breaks on these in
# addition to \n and \r\n. A file holding one would come back with extra newlines
# where the bytes had none, so the editor looks for them before it lets Ctrl+S
# anywhere near the disk.
EXOTIC_BREAKS = ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")

DIRTY_MARK = "●"          # the unsaved dot in the title bar


# ------------------------------------------------------------------ app handle --

def running_app():
    """The Textual app this code is standing inside, or None.

    `/edit-ui` reaches the screen through the app that is already running rather
    than through a reference the plugin does not own. `active_app` is a
    ContextVar, so this only answers inside the app's own task tree — which is
    exactly where a command handler runs when the TUI dispatches a typed line,
    and exactly where the classic REPL does not have one.
    """
    try:
        from textual.app import active_app

        return active_app.get()
    except Exception:                             # no Textual, no app, no loop
        return None


# ----------------------------------------------------------------- undo journal --

def journal_module():
    """`beeagent.core.journal`, or None when there is no undo journal.

    Guarded for the three reasons `beeagent/tools/files.py` documents for the
    same helper: the module may be absent, may not import, or may not carry
    `record`. None of them means "go ahead and write" — a change whose previous
    bytes are not stored is a change this editor will not make.
    """
    try:
        from beeagent.core import journal
    except Exception:                             # noqa: BLE001 - absence is the answer
        return None
    return journal if callable(getattr(journal, "record", None)) else None


def journal_record(workdir: str, path) -> tuple[bool, str]:
    """(recorded, what to tell the user) for one save; False means write nothing.

    Called once per save, before the bytes change, so that `/undo` finds the
    previous content of the file rather than the half-written new one.
    """
    journal = journal_module()
    if journal is None:
        return False, L(
            "the undo journal does not load, so these bytes could not be put back "
            "again — nothing was written and your typing is still on screen",
            "журнал отмены не загружается, эти байты не вернуть — ничего не записано, "
            "ваши правки на экране")
    try:
        entry = journal.record(workdir, "edit-ui", str(path), action="modify")
    except Exception as e:                        # noqa: BLE001 - any refusal stops the save
        refusal = getattr(journal, "refusal", None)
        if callable(refusal):
            return False, str(refusal("edit-ui", e))
        return False, L(f"the undo journal refused the change ({e}); nothing was written",
                        f"журнал отмены отказал ({e}); ничего не записано")
    clause = getattr(journal, "recoverable_clause", None)
    note = str(clause(entry)) if callable(clause) else ""
    return True, note.strip(" —") or L("previous bytes saved", "прежние байты сохранены")


# ------------------------------------------------------------------ file access --

def resolved_target(raw, action: str = "edit-ui open") -> tuple[Path | None, str]:
    """(path, refusal) — the file policy's own answer, unchanged.

    `tools/_path_policy.guard` is the single place that decides where BeeCode may
    go, and it decides from `realpath` rather than from the spelling. Reusing it
    means this editor cannot be pointed outside the project by a path the model
    retyped, and that the refusal names the resolved file in the same words the
    file tools use. Used read-only: nothing here widens it or edits it.
    """
    from beeagent.tools._path_policy import guard

    return guard(raw, action=action)


def scan_breaks(text: str) -> tuple[bool, bool]:
    """(holds_a_break_that_cannot_be_rebuilt, mixes_two_line_ending_styles).

    The first answer is the dangerous one: Textual splits a document with
    `str.splitlines()`, which also breaks on a form feed, a lone CR, U+2028 and
    the rest, and rejoins every one of them as an ordinary newline. Those bytes
    cannot be put back, so a file holding them is opened for reading only.
    """
    lone_cr = "\r" in text.replace("\r\n", "")
    exotic = any(ch in text for ch in EXOTIC_BREAKS)
    if lone_cr or exotic:
        return True, False
    endings = {"\r\n" if line.endswith("\r\n") else "\n"
               for line in text.splitlines(keepends=True) if line}
    return False, len(endings) > 1


@dataclass
class Loaded:
    """One file, as much of it as the editor is willing to hold."""
    path: Path
    text: str                      # the window, exactly as many bytes as it came in
    lines: int                     # lines of that window
    size: int                      # bytes in the whole file
    window_bytes: int              # bytes of the file this window is
    tail_offset: int | None = None  # where the part the editor never read begins
    truncated: bool = False
    structural_breaks: bool = False
    mixed_newlines: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def can_save(self) -> bool:
        """Whether the widget's document can be turned back into these bytes."""
        return not self.structural_breaks

    @property
    def refusal(self) -> str:
        if not self.structural_breaks:
            return ""
        found = ", ".join(sorted({f"U+{ord(ch):04X}" for ch in
                                  [c for c in self.text if c in EXOTIC_BREAKS]}
                                 | ({"U+000D"} if "\r" in self.text.replace("\r\n", "")
                                    else set())))
        return L(
            f"this file holds a line break ({found or 'U+000D'}) that the editor "
            f"splits but cannot put back, so Ctrl+S is refused rather than guessed "
            f"with: read it with the read tool and change it with the edit tool",
            f"в файле есть разрыв строки ({found or 'U+000D'}), который редактор "
            f"разбивает, но не собирает обратно: Ctrl+S отказывает, а не угадывает — "
            f"читайте read-инструментом, правьте edit-инструментом")

    @property
    def tail_bytes(self) -> int:
        if self.tail_offset is None:
            return 0
        return max(0, self.size - self.tail_offset)

    def tail_text(self) -> str:
        """The bytes below the window, verbatim. Raises OSError when they cannot be had."""
        if self.tail_offset is None:
            return ""
        with open(str(self.path), "rb") as handle:
            handle.seek(self.tail_offset)
            return handle.read().decode("utf-8")


def _line_count(text: str) -> int:
    """How many lines a window holds: the trailing newline does not start one."""
    return len(text.splitlines())


def _decode_window(body: bytes) -> str:
    """The longest whole-character prefix of a byte window.

    A cap that lands in the middle of a Cyrillic character must not turn that
    character into U+FFFD; the byte is dropped from the window instead, and the
    tail it belongs to is copied back through untouched on save.
    """
    for trim in range(0, 5):
        chunk = body[:len(body) - trim] if trim else body
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return body.decode("utf-8", errors="replace")


def _first_lines(text: str, limit: int) -> str:
    """The first `limit` lines of `text`, each with the terminator it already had.

    A window is cut by re-taking its own prefix, never by joining lines with a
    newline of the editor's choosing: the byte after the last kept line is the
    first byte of the tail, and inventing a separator there would duplicate one
    on save.
    """
    kept: list[str] = []
    position = 0
    length = len(text)
    while len(kept) < limit and position < length:
        newline = text.find("\n", position)
        if newline < 0:
            kept.append(text[position:])                  # the last line, unterminated
            position = length
        else:
            kept.append(text[position:newline + 1])
            position = newline + 1
    return "".join(kept)


def load_file(path, max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> Loaded:
    """Read the first window of a file without reading all of a big one.

    A file inside the cap is read whole through `read_text_preserving` — the only
    read path in this project that keeps a file's own line endings and refuses to
    invent replacement characters. A bigger one is read in binary up to the cap
    and cut at a line boundary under the same strict-UTF-8 rule: a file the
    process cannot decode raises, and the caller reports it instead of opening an
    empty buffer. A file whose *first* line is longer than the window has no line
    boundary to cut at, so the window is that partial line and the tail begins
    inside it; the two are joined again byte for byte on save.
    """
    size = os.path.getsize(str(path))
    if size <= max_bytes:
        text = read_text_preserving(str(path))
        truncated = False
    else:
        with open(str(path), "rb") as handle:
            text = _decode_window(handle.read(max_bytes))
        cut = text.rfind("\n") + 1               # never end inside a half line
        text = text[:cut] if cut else text
        truncated = True
    if _line_count(text) > max_lines:
        text = _first_lines(text, max_lines)
        truncated = True
    window_bytes = len(text.encode("utf-8"))
    loaded = Loaded(path=Path(path), text=text, lines=_line_count(text), size=size,
                    window_bytes=window_bytes, truncated=truncated,
                    tail_offset=window_bytes if truncated else None)
    loaded.structural_breaks, loaded.mixed_newlines = scan_breaks(text)
    return loaded


def display_name(path, workdir: str = ".") -> str:
    """The path the way the user thinks about it: relative when it is inside."""
    try:
        root = Path(os.path.realpath(os.path.abspath(str(workdir or "."))))
        real = Path(os.path.realpath(os.path.abspath(str(path))))
        return real.relative_to(root).as_posix()
    except (OSError, ValueError):
        return str(path)


def _stamp(path) -> tuple[int, int] | None:
    """(mtime_ns, size) — the cheap answer to "is this still the file I read?"."""
    try:
        info = os.stat(str(path))
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size)


# --------------------------------------------------------------------- widgets --

class HiveTextArea(TextArea):
    """A text area that owns two letters the binding system will not hand it.

    `n` and `N` mean "next/previous match" while a search is armed, and must not
    be typed into the file. They cannot be `Binding`s: `Screen._binding_chain`
    drops any key the focused widget consumes, and a text area consumes every
    printable character — so a screen-level `n` would silently vanish and the
    letter would land in the buffer anyway. Filtering the event here, before the
    widget inserts it, is the only place both rules are satisfiable.
    """

    async def _on_key(self, event: events.Key) -> None:
        try:
            screen = self.screen
        except Exception:                           # noqa: BLE001 - unmounted mid-key
            screen = None
        owner = screen if isinstance(screen, EditorScreen) else None
        if event.key == "escape":
            # A text area in indent-tab mode eats Escape and walks the focus
            # instead. Here Escape means "cancel the prompt, or close", and both
            # answers belong to the screen, so the event is left to bubble.
            return
        if event.key in ("n", "N") and owner is not None and owner.search_armed:
            event.stop()
            event.prevent_default()
            owner.find_next(reverse=event.key == "N")
            return
        await super()._on_key(event)


class DiscardScreen(ModalScreen):
    """The one question a closing editor is allowed to ask.

    Three answers, and the safest is the one that is highlighted and the one an
    absent-minded Enter reaches. The typed text stays on the screen underneath
    the whole time: nothing is discarded by accident because nothing is discarded
    by default.
    """

    CSS = f"""
    #ed-ask-back {{
        width: 1fr;
        height: 1fr;
        align: center middle;
        background: {HIVE_BACKDROP};
    }}
    #ed-ask {{
        width: 80;
        max-width: 90%;
        height: auto;
        padding: 1 2;
        background: {HIVE_PANEL};
        border: heavy {LEAF};
    }}
    #ed-ask-body {{ width: 1fr; color: {HONEY}; margin-top: 1; }}
    #ed-ask-hint {{ width: 1fr; color: {HIVE_ROW}; margin-top: 1; }}
    """

    BINDINGS = [
        Binding("escape", "keep", "Keep editing", priority=True),
        Binding("ctrl+q", "keep", "Keep editing", priority=True),
        Binding("k", "keep", "Keep editing"),
        Binding("ctrl+s", "save_and_close", "Save and close", priority=True),
        Binding("d", "discard", "Discard"),
    ]

    def __init__(self, summary: str) -> None:
        super().__init__()
        self._summary = summary

    def compose(self) -> ComposeResult:
        with Vertical(id="ed-ask-back"):
            with Vertical(id="ed-ask"):
                yield Static(bee_title(L("unsaved changes", "несохранённые правки")))
                yield Static(self._summary, id="ed-ask-body")
                yield Static(L("Ctrl+S saves and closes · d throws the typing away · "
                               "Esc goes back to it",
                               "Ctrl+S сохраняет и закрывает · d выбрасывает правки · "
                               "Esc возвращает к тексту"), id="ed-ask-hint")

    def action_keep(self) -> None:
        self.dismiss(False)

    def action_discard(self) -> None:
        self.dismiss(True)

    def action_save_and_close(self) -> None:
        self.dismiss("save")


class EditorScreen(ModalScreen):
    """A full-screen, line-numbered, editable view of one file.

    A `ModalScreen` on purpose: Textual cuts the key-binding chain at the topmost
    modal screen, so BeeCodeApp's own `ctrl+q` (which quits the whole program)
    cannot fire while a human is editing — no monkeypatching of the host app is
    needed, and none of its keys are stolen either.
    """

    CSS = f"""
    #ed-back {{
        width: 1fr;
        height: 1fr;
        background: {HIVE_BACKDROP};
        padding: 0 1;
    }}
    #ed-body {{ width: 1fr; height: 1fr; }}
    #ed-title {{
        width: 1fr;
        height: 1;
        background: {HIVE_PANEL};
        padding: 0 1;
        border-bottom: heavy {DARK_LEAF};
    }}
    #ed-area {{
        width: 1fr;
        height: 1fr;
        border: heavy {DARK_LEAF};
        background: {HIVE_PANEL};
        color: {HIVE_ROW};
        scrollbar-color: {HONEY};
        scrollbar-color-active: {HONEY};
        scrollbar-color-hover: {LEAF};
        scrollbar-background: {HIVE_BACKDROP};
    }}
    #ed-area:focus {{ border: heavy {LEAF}; }}
    #ed-area .text-area--gutter {{ color: {LEAF}; background: {HIVE_BACKDROP}; }}
    #ed-area .text-area--cursor-gutter {{ color: {HIVE_BACKDROP}; background: {HONEY};
                                          text-style: bold; }}
    #ed-area .text-area--cursor-line {{ background: {HIVE_LINE}; }}
    #ed-area .text-area--cursor {{ color: {HIVE_BACKDROP}; background: {HONEY}; }}
    #ed-area .text-area--selection {{ background: {HONEY}; color: {HIVE_BACKDROP}; }}
    #ed-prompt-row {{
        width: 1fr;
        height: auto;
        display: none;
        background: {HIVE_PANEL};
        padding: 0 1;
    }}
    #ed-prompt-row.shown {{ display: block; }}
    #ed-prompt-label {{ width: 12; color: {HONEY}; text-style: bold; }}
    #ed-prompt {{
        width: 1fr;
        background: {HIVE_FIELD};
        color: {HIVE_ROW};
        border: tall {DARK_LEAF};
    }}
    #ed-prompt:focus {{ border: tall {LEAF}; }}
    #ed-status {{
        width: 1fr;
        height: auto;
        max-height: 4;
        background: {HIVE_PANEL};
        color: {HIVE_ROW};
        padding: 0 1;
        border-top: heavy {DARK_LEAF};
    }}
    #ed-keys {{
        width: 1fr;
        height: 1;
        background: {HIVE_BACKDROP};
        color: {LEAF};
        padding: 0 1;
    }}
    """

    # `priority` on every one of them, because two of these keys belong to
    # somebody else otherwise: `ctrl+q` is BeeCodeApp's "quit the program", and
    # `ctrl+k` is the text area's kill-to-end-of-line. Cutting to end of line
    # stays available as shift+End, Delete; deleting a whole line stays
    # ctrl+shift+K. The strip at the bottom of the screen says which is which.
    BINDINGS = [
        Binding("ctrl+s", "save", "Save", priority=True),
        Binding("ctrl+q", "close", "Close", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("ctrl+f", "find", "Find", priority=True),
        Binding("ctrl+g", "goto_line", "Go to line", priority=True),
        Binding("ctrl+k", "goto_percent", "Go to %", priority=True),
    ]

    def __init__(self, loaded: Loaded, workdir: str = ".") -> None:
        super().__init__()
        self.loaded = loaded
        self.path = loaded.path
        self.workdir = workdir
        self._saved_text = loaded.text
        self._stamp = _stamp(loaded.path)
        self._unexplained = False
        self.search_term = ""
        self.matches: list[tuple[int, int]] = []
        self.match_index = 0
        self.search_armed = False
        self.status = ""
        self.prompt_mode = ""
        self.saves = 0
        self.written_bytes: int | None = None
        self.discarded = False

    # --- layout ------------------------------------------------------------
    def compose(self) -> ComposeResult:
        with Vertical(id="ed-back"):
            with Vertical(id="ed-body"):
                yield Static("", id="ed-title")
                yield HiveTextArea("", id="ed-area", show_line_numbers=True,
                                   soft_wrap=False, tab_behavior="indent")
                with Horizontal(id="ed-prompt-row"):
                    yield Label("", id="ed-prompt-label")
                    yield Input(id="ed-prompt")
                yield Static("", id="ed-status")
                yield Static(self._keys_line(), id="ed-keys")

    def on_mount(self) -> None:
        area = self.query_one("#ed-area", HiveTextArea)
        area.load_text(self.loaded.text)
        # The widget rebuilds the text from its own document; if it hands back
        # something other than the bytes that came off the disk, saving it is a
        # rewrite and not an edit. `mixed_newlines` says we already know that and
        # warned about it; anything else is a reason to refuse.
        rejoined = area.text
        self._saved_text = rejoined
        self._unexplained = (self.loaded.can_save and not self.loaded.mixed_newlines
                             and rejoined != self.loaded.text)
        self.refresh_chrome()
        area.focus()

    # --- what the user reads ------------------------------------------------
    @property
    def buffer_text(self) -> str:
        return self.query_one("#ed-area", HiveTextArea).text

    @property
    def dirty(self) -> bool:
        return self.buffer_text != self._saved_text

    @property
    def can_save(self) -> bool:
        return self.loaded.can_save and not self._unexplained

    def title_text(self) -> str:
        """The same words the title bar draws, for anyone who would rather not parse it."""
        name = display_name(self.path, self.workdir)
        return (f"🐝 edit-ui  {name}  {DIRTY_MARK} {L('unsaved', 'не сохранено')}"
                if self.dirty else
                f"🐝 edit-ui  {name}  ✓ {L('saved', 'сохранено')}")

    def _keys_line(self) -> str:
        return L("↑↓ PgUp/PgDn Home/End scroll · Ctrl+G line · Ctrl+K % · "
                 "Ctrl+F find (n next, N back) · Ctrl+S save · Ctrl+Q/Esc close",
                 "↑↓ PgUp/PgDn Home/End листать · Ctrl+G строка · Ctrl+K % · "
                 "Ctrl+F поиск (n далее, N назад) · Ctrl+S сохранить · Ctrl+Q/Esc закрыть")

    def status_text(self) -> str:
        """One honest block: how big the window is, and what the last key did."""
        loaded = self.loaded
        shown = display_name(loaded.path, self.workdir)
        if loaded.truncated:
            bits = [L(f"loaded {loaded.lines} line(s) — {loaded.window_bytes} of "
                      f"{loaded.size} bytes of this file — the rest is outside this "
                      f"window and is not searched, shown or renumbered here; use the "
                      f"read tool for it (read {shown} offset=…)",
                      f"загружено {loaded.lines} строк — {loaded.window_bytes} из "
                      f"{loaded.size} байт файла — остальное вне этого окна, здесь оно "
                      f"не ищется и не показывается; для него read-инструмент "
                      f"(read {shown} offset=…)")]
        else:
            bits = [L(f"{loaded.lines} line(s) · {loaded.size} byte(s)",
                      f"{loaded.lines} строк · {loaded.size} байт")]
        if loaded.mixed_newlines:
            bits.append(L("this file mixes CRLF and LF: a save rewrites every line in "
                          "the style the editor detected",
                          "в файле смешаны CRLF и LF: сохранение перепишет все строки "
                          "в найденный редактором стиль"))
        if not self.can_save:
            bits.append(loaded.refusal or L(
                "these line endings cannot be rebuilt byte for byte, so Ctrl+S is "
                "refused; nothing you type here will be lost, it just cannot be saved "
                "from here",
                "эти окончания строк нельзя собрать байт в байт, Ctrl+S откажет; "
                "набранное не потеряется, но отсюда его не сохранить"))
        if self.status:
            bits.append(self.status)
        return "\n".join(bit for bit in bits if bit)

    def refresh_chrome(self) -> None:
        name = display_name(self.path, self.workdir)
        tail = (f"  {DIRTY_MARK} " + L("unsaved", "не сохранено") if self.dirty
                else "  ✓ " + L("saved", "сохранено"))
        style = f"bold {HONEY}" if self.dirty else f"bold {LEAF}"
        self.query_one("#ed-title", Static).update(
            Text.assemble(bee_title(f"🐝 edit-ui  {name}"), (tail, style)))
        self.title = name
        self.query_one("#ed-status", Static).update(Text(self.status_text()))

    def say(self, message: str) -> None:
        """One line about what just happened, said in the status strip."""
        self.status = message
        self.refresh_chrome()

    def on_text_area_changed(self, event) -> None:
        self.refresh_chrome()

    # --- seeking ------------------------------------------------------------
    def action_goto_line(self) -> None:
        line = self.query_one("#ed-area", HiveTextArea).cursor_location[0] + 1
        self._open_prompt(L("line:", "строка:"), mode="line", hint=str(line))

    def action_goto_percent(self) -> None:
        self._open_prompt(L("jump %:", "переход %:"), mode="percent", hint="")

    def action_find(self) -> None:
        self._open_prompt(L("find:", "искать:"), mode="find", hint=self.search_term)

    def _open_prompt(self, label: str, mode: str, hint: str) -> None:
        self.prompt_mode = mode
        row = self.query_one("#ed-prompt-row")
        row.display = True
        row.add_class("shown")
        self.query_one("#ed-prompt-label", Label).update(Text(label, style=f"bold {HONEY}"))
        box = self.query_one("#ed-prompt", Input)
        box.value = hint
        box.focus()

    def _close_prompt(self) -> None:
        self.prompt_mode = ""
        row = self.query_one("#ed-prompt-row")
        row.display = False
        row.remove_class("shown")
        self.query_one("#ed-area", HiveTextArea).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "ed-prompt":
            return
        value, mode = event.value, self.prompt_mode
        self._close_prompt()
        if mode == "find":
            self.run_search(value)
        elif mode == "line":
            self.goto_line(value)
        elif mode == "percent":
            self.goto_percent(value)

    def _loaded_lines(self) -> list[str]:
        return self.query_one("#ed-area", HiveTextArea).document.lines

    def goto_line(self, raw: str) -> None:
        area = self.query_one("#ed-area", HiveTextArea)
        digits = "".join(ch for ch in raw if ch.isdigit())
        total = len(area.document.lines)
        if not digits:
            self.say(L("type a line number, e.g. 42", "введите номер строки, например 42"))
            return
        number = int(digits)
        if not 1 <= number <= total:
            self.say(L(f"this window holds lines 1–{total}; line {number} is not among "
                       f"them — the file may well continue past the window",
                       f"в этом окне строки 1–{total}; строки {number} в них нет — "
                       f"файл может продолжаться за окном"))
            return
        area.move_cursor((number - 1, 0), center=True)
        self.say(L(f"line {number} of {total} loaded", f"строка {number} из {total} загруженных"))

    def goto_percent(self, raw: str) -> None:
        """A proportion seek, because on a capped window "End" is not the end."""
        area = self.query_one("#ed-area", HiveTextArea)
        digits = "".join(ch for ch in raw if ch.isdigit())
        total = len(area.document.lines)
        if not digits or not total:
            self.say(L("type how far into the window to jump, 0 to 100",
                       "введите, как далеко внутри окна прыгать, от 0 до 100"))
            return
        percent = max(0, min(100, int(digits)))
        line = max(0, min(total - 1, round((percent / 100) * (total - 1))))
        area.move_cursor((line, 0), center=True)
        self.say(L(f"{percent}% of the window is line {line + 1} — the file itself "
                   f"continues past it",
                   f"{percent}% окна — это строка {line + 1}; сам файл идёт дальше"))

    # --- search -------------------------------------------------------------
    def run_search(self, term: str) -> None:
        """Find `term` in the loaded window and put the cursor on the first hit.

        Case-insensitive unless the query itself holds a capital, which is the
        rule with the fewest surprises in a tool that has no search options to
        explain. The window is the whole search space, and the status line says
        so when the answer is "nothing" — a miss inside 5,000 lines of a 40 MB
        log is not a claim that the file does not contain it.
        """
        self.search_term = term
        lines = self._loaded_lines()
        if not term:
            self.matches, self.search_armed = [], False
            self.say(L("empty search — nothing was looked for",
                       "пустой поиск — ничего не искали"))
            return
        fold = term == term.lower()
        needle = term.lower() if fold else term
        hits: list[tuple[int, int]] = []
        for number, line in enumerate(lines):
            haystack = line.lower() if fold else line
            at = haystack.find(needle)
            while at >= 0:
                hits.append((number, at))
                at = haystack.find(needle, at + len(needle))
        area = self.query_one("#ed-area", HiveTextArea)
        cursor = area.cursor_location[0]
        hits.sort(key=lambda hit: (hit[0] < cursor, hit[0], hit[1]))
        self.matches = hits
        self.match_index = 0
        if not hits:
            self.search_armed = False
            self.say(L(f"no match for “{term}” in the {len(lines)} loaded line(s)",
                       f"«{term}» не найдено в {len(lines)} загруженных строках"))
            return
        self.show_match()

    def find_next(self, reverse: bool = False) -> None:
        if not self.matches:
            self.say(L("nothing is searched for yet — Ctrl+F first",
                       "пока нечего искать — сначала Ctrl+F"))
            return
        self.match_index = (self.match_index + (-1 if reverse else 1)) % len(self.matches)
        self.show_match()

    def show_match(self) -> None:
        line, column = self.matches[self.match_index]
        area = self.query_one("#ed-area", HiveTextArea)
        area.move_cursor((line, column), center=True)
        self.search_armed = True
        self.say(L(f"“{self.search_term}” — match {self.match_index + 1} of "
                   f"{len(self.matches)}, line {line + 1} (n next, N back)",
                   f"«{self.search_term}» — совпадение {self.match_index + 1} из "
                   f"{len(self.matches)}, строка {line + 1} (n далее, N назад)"))

    # --- save ---------------------------------------------------------------
    def action_save(self) -> None:
        self.save()

    def body_for_disk(self) -> tuple[str | None, str]:
        """The exact text Ctrl+S would write, or the reason it writes nothing.

        A capped window is spliced back onto the part of the file the editor
        never read, so saving the first 5,000 lines of a 200,000-line log cannot
        delete the other 195,000.
        """
        loaded = self.loaded
        if not self.can_save:
            return None, (loaded.refusal or L(
                "the editor cannot rebuild these bytes, so it will not write them",
                "редактор не может собрать эти байты и потому не пишет их"))
        head = self.buffer_text
        if loaded.tail_offset is None:
            return head, ""
        tail_size = loaded.tail_bytes
        if tail_size > MAX_TAIL_BYTES:
            return None, L(
                f"this file is {loaded.size} bytes and the editor holds "
                f"{loaded.window_bytes} of them; saving would mean pulling the other "
                f"{tail_size} bytes through memory, so nothing was written — ask the "
                f"agent to edit it instead",
                f"файл весит {loaded.size} байт, редактор держит {loaded.window_bytes}; "
                f"сохранять, прогоняя через память ещё {tail_size}, не будем — ничего "
                f"не записано. Попросите агента править его")
        try:
            tail = loaded.tail_text()
        except (OSError, UnicodeDecodeError) as e:
            return None, L(f"the rest of the file could not be read in order to be put "
                           f"back ({e}); nothing was written",
                           f"остаток файла не удалось прочитать, чтобы вернуть его "
                           f"({e}); ничего не записано")
        return head + tail, L(f"the {tail_size} bytes below the window were copied "
                              f"through untouched",
                              f"{tail_size} байт под окном прописаны без изменений")

    def save(self, close_after: bool = False) -> bool:
        """Journal first, then the atomic write. True when the disk changed."""
        if not self.dirty:
            self.say(L("nothing has changed since the last save",
                       "с последнего сохранения ничего не изменилось"))
            if close_after:
                self.dismiss(None)
            return False
        body, note = self.body_for_disk()
        if body is None:
            self.say(note)
            return False
        if _stamp(self.path) != self._stamp:
            self.say(L("the file changed on disk after it was opened, so nothing was "
                       "written over it — close and open it again to see the new bytes; "
                       "your typing is still here",
                       "файл на диске изменился после открытия, поверх него ничего не "
                       "пишется — закройте и откройте заново; ваши правки на месте"))
            return False
        recorded, why = journal_record(self.workdir, self.path)
        if not recorded:
            self.say(why)
            return False
        try:
            written = write_text_preserving(str(self.path), body)
        except OSError as e:
            # `write_text_preserving` renames over the target, so a failure here
            # means the file still holds the bytes the journal just stored.
            self.say(L(f"the write failed ({e.__class__.__name__}: {e}) — the file "
                       f"still holds its previous bytes and your typing is still here",
                       f"запись не удалась ({e.__class__.__name__}: {e}) — в файле "
                       f"прежние байты, ваши правки на экране"))
            return False
        self.saves += 1
        self.written_bytes = written
        self._saved_text = self.buffer_text
        self._stamp = _stamp(self.path)
        self.loaded.window_bytes = len(self.buffer_text.encode("utf-8"))
        self.loaded.lines = len(self._loaded_lines())
        message = L(f"saved {written} bytes · {why}", f"сохранено {written} байт · {why}")
        self.say(f"{message} · {note}" if note else message)
        if close_after:
            self.dismiss(None)
        return True

    # --- close --------------------------------------------------------------
    def action_cancel(self) -> None:
        """Esc: undo the last thing the user asked for, which may only be the prompt."""
        if self.prompt_mode:
            self._close_prompt()
            return
        self.request_close()

    def action_close(self) -> None:
        self.request_close()

    def request_close(self) -> None:
        """Never discard silently: one question, asked once, with the text still visible."""
        if not self.dirty:
            self.dismiss(None)
            return
        name = display_name(self.path, self.workdir)
        # A screen has no `push_screen`; the app owns the stack, and the callback
        # is the only way the answer comes back.
        self.app.push_screen(DiscardScreen(L(
            f"You have typed since the last save of {name}. Nothing has been written, "
            f"and nothing will be until you choose.",
            f"Вы печатали после сохранения {name}. Ничего не записано и не будет, "
            f"пока вы не выберете.")), self._answered)

    def _answered(self, answer) -> None:
        if answer == "save":
            self.save(close_after=True)     # a refusal leaves the screen up, on purpose
        elif answer is True:
            self.discarded = True
            self.dismiss(None)
        else:
            self.refresh_chrome()           # kept editing; the buffer never moved


# ------------------------------------------------------------------ entry point --

def open_editor(raw: str, workdir: str | None = None, app=None,
                max_lines: int = MAX_LINES, max_bytes: int = MAX_BYTES) -> str:
    """Put `<path>` on screen. Returns the message the user has to read, or "" .

    Every refusal leaves BeeCode exactly as it was: no screen, no write, and a
    line that says which path was meant.
    """
    workdir = workdir or os.getcwd()
    app = app if app is not None else running_app()
    if app is None:
        return L(
            "/edit-ui needs the full-screen interface — start BeeCode with `beecode` "
            "(the Textual TUI) and ask again; the classic REPL has no screen to open "
            "an editor on",
            "/edit-ui нужен полнотэкранный интерфейс — запустите BeeCode с "
            "`beecode` (TUI на Textual); у классического REPL нет экрана для редактора")
    text = (raw or "").strip()
    if not text:
        return L("usage: /edit-ui <path> — name one file to open",
                 "употребление: /edit-ui <путь> — назовите файл")
    target, refusal = resolved_target(text)
    if refusal:
        return refusal
    if not target.exists():
        return L(f"there is no file at {target}", f"файла {target} нет")
    if target.is_dir():
        return L(f"{target} is a directory — the editor opens one file at a time",
                 f"{target} — папка; редактор открывает по одному файлу")
    try:
        loaded = load_file(target, max_lines=max_lines, max_bytes=max_bytes)
    except (OSError, UnicodeDecodeError, ValueError) as e:
        # Not "an empty file". A buffer with nothing in it that the user then
        # saves would answer the one question this tool must never answer wrong:
        # what was in the file.
        return L(f"{target} could not be read ({e.__class__.__name__}: {e}) — it was "
                 f"not opened, and nothing was changed",
                 f"{target} не удалось прочитать ({e.__class__.__name__}: {e}) — "
                 f"не открыто, ничего не изменено")
    app.push_screen(EditorScreen(loaded, workdir=workdir))
    return ""
