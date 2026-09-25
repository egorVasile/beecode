"""The `/edit-ui` file editor, driven headlessly through a real Textual pilot.

Nothing here mocks the widget: every scenario starts an app at 120x40, pushes the
editor screen with the same call the plugin's command makes, presses real keys,
and then looks at the *bytes on disk*. An assertion on the buffer alone would keep
passing while the file was being rewritten with LF endings, and preserving the
file's own endings is the one thing this editor exists to guarantee.

`HostApp` is deliberately not BeeCodeApp. It is the minimal host that carries the
same four app-level bindings as `ui/tui.py` `BeeCodeApp.BINDINGS` — those bindings
are precisely what the editor's own keys have to survive — and it costs an agent,
a provider and a network it does not need.

Plugin tests work in a folder of their own with `monkeypatch.chdir`, because
extensions are discovered in `./.beeagent` and the path policy resolves against
the process cwd.
"""
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from textual.app import App, Binding
from textual.widgets import Label

from beeagent.ui import editor
from beeagent.ui.components import DARK_LEAF, HONEY, LEAF
from beeagent.ui.editor import DIRTY_MARK, EditorScreen, HIVE_BACKDROP, HIVE_PANEL

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = REPO_ROOT / "beeagent" / "plugins" / "templates" / "plugins"

# Cyrillic content, Windows line endings, a trailing newline: the three things a
# naive read/write cycle breaks at once.
BODY = ("заголовок\r\n"
        "вторая строка, которую надо поправить\r\n"
        "третья строка\r\n"
        "четвёртая\r\n")

# A measured number that belongs in the report rather than in an assertion.
TIMINGS: dict[str, float] = {}


class HostApp(App):
    """The smallest host that can carry a screen — with BeeCodeApp's own bindings."""

    BINDINGS = [
        Binding("ctrl+q", "quit_app", "Quit"),
        Binding("ctrl+l", "clear_log", "Clear"),
        Binding("ctrl+b", "toggle_sidebar", "Sidebar"),
        Binding("f1", "help", "Help"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.quits = 0

    def compose(self):
        yield Label("host", id="host")

    def action_quit_app(self) -> None:
        self.quits += 1
        self.exit()

    def action_clear_log(self) -> None:
        pass

    def action_toggle_sidebar(self) -> None:
        pass

    def action_help(self) -> None:
        pass


def write(path: Path, text: str) -> Path:
    """Bytes exactly as given: no translation, no invented trailing newline."""
    path.write_bytes(text.encode("utf-8"))
    return path


# --------------------------------------------------------------------------- 1 --

def test_open_scroll_search_edit_and_save_keep_crlf_and_cyrillic(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            assert editor.open_editor("notes.txt", workdir=str(tmp_path)) == ""
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, EditorScreen)
            area = screen.query_one("#ed-area")
            assert area.document.newline == "\r\n"
            assert area.document.lines[1] == "вторая строка, которую надо поправить"
            assert screen.query_one("#ed-title").render().plain == screen.title_text()
            assert DIRTY_MARK not in screen.title_text()
            assert screen.dirty is False

            # scrolling: the cursor moves, the viewport follows, and nothing types
            before = area.text
            await pilot.press("down", "down")
            assert area.cursor_location[0] == 2
            await pilot.press("end")
            assert area.cursor_location == (2, len(area.document.lines[2]))
            await pilot.press("home")
            assert area.cursor_location == (2, 0)
            await pilot.press("pagedown")
            assert area.cursor_location[0] > 2
            await pilot.press("pageup")
            assert area.cursor_location[0] == 0
            await pilot.press("ctrl+k")                        # whole-window seek
            await pilot.press("1")
            await pilot.press("enter")
            assert area.cursor_location[0] == 0
            assert area.text == before
            await pilot.press("ctrl+g")                  # jump to a line number
            await pilot.press("2", "enter")
            assert area.cursor_location[0] == 1
            assert "line 2" in screen.status
            await pilot.press("ctrl+k")                  # jump by proportion
            await pilot.press("5", "0", "enter")
            assert area.cursor_location[0] == 2

            # search: Ctrl+F, a Cyrillic query, n / N through the matches. The
            # first hit is the one nearest where the reader already is (the cursor
            # is on line 3 after the proportion jump above), not the top of the file.
            await pilot.press("ctrl+f")
            await pilot.press(*list("строка"))
            await pilot.press("enter")
            assert screen.matches == [(2, 7), (1, 7)]
            assert screen.search_armed
            assert area.cursor_location == (2, 7)
            await pilot.press("n")
            assert area.cursor_location == (1, 7)
            await pilot.press("N")
            assert area.cursor_location == (2, 7)
            assert "match 1 of 2" in screen.status

            # edit: a real keystroke at the end of the line the search last showed
            await pilot.press("end")
            await pilot.press("!", "enter", "о")
            assert screen.dirty
            assert DIRTY_MARK in screen.title_text()
            assert "unsaved" in screen.title_text()

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 1
            assert screen.dirty is False
            assert DIRTY_MARK not in screen.title_text()
        on_disk = (tmp_path / "notes.txt").read_bytes()
        assert on_disk.startswith("заголовок\r\n".encode("utf-8"))
        assert on_disk.endswith("четвёртая\r\n".encode("utf-8"))
        # The split happened on the line the search was showing: "третья строка"
        # became that line plus "!" and a new line starting with "о".
        assert "третья строка!\r\nо\r\n".encode("utf-8") in on_disk
        assert "вторая строка, которую надо поправить\r\n".encode("utf-8") in on_disk
        assert on_disk.count(b"\r\n") == 5
        assert b"\n" not in on_disk.replace(b"\r\n", b""), "no LF-only line appeared"
        assert "заголовок".encode("utf-8") in on_disk and "четвёртая".encode("utf-8") in on_disk

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 2 --

def test_unsaved_close_asks_once_and_keeping_the_typing_costs_nothing(tmp_path,
                                                                     monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            area = screen.query_one("#ed-area")
            await pilot.press("ctrl+g", "1", "enter")
            await pilot.press("X")
            typed = area.text
            assert screen.dirty

            await pilot.press("ctrl+q")
            await pilot.pause()
            assert type(app.screen).__name__ == "DiscardScreen"
            assert app.quits == 0, "closing the editor must never reach the app's quit"
            assert area.text == typed, "the question must not touch the buffer"

            await pilot.press("escape")                       # keep editing
            await pilot.pause()
            assert isinstance(app.screen, EditorScreen)
            assert app.screen.query_one("#ed-area").text == typed
            assert screen.dirty

            await pilot.press("ctrl+q")                       # ask again, then discard
            await pilot.pause()
            await pilot.press("d")
            await pilot.pause()
            assert app.screen is not screen
            assert screen.discarded
            assert app.quits == 0
        assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")

    asyncio.run(scenario())


def test_the_question_can_save_and_close_in_one_answer(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#ed-area").insert("!")
            await pilot.press("ctrl+q")                        # close while dirty
            await pilot.pause()
            await pilot.press("ctrl+s")                        # ...and answer "save it"
            await pilot.pause()
            assert screen.saves == 1
            assert app.screen is not screen
        assert (tmp_path / "notes.txt").read_bytes() == ("!" + BODY).encode("utf-8")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 3 --

def test_a_path_outside_the_project_is_refused_with_its_resolved_name(tmp_path,
                                                                     monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    outside = write(tmp_path / "secrets.txt", "ключ\r\n")
    monkeypatch.chdir(project)
    # `_path_policy` trusts the working directory *and* the interpreter's temp
    # directory, and pytest puts tmp_path inside the latter. The only honest way
    # to ask "what about a file outside every root?" is to move the temp root
    # into the project for the length of the test; the policy itself is untouched.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(project))

    async def scenario():
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            message = editor.open_editor(str(outside), workdir=str(project))
            await pilot.pause()
            assert "OUTSIDE" in message
            assert str(outside.resolve()).lower().replace("\\", "/") in \
                message.lower().replace("\\", "/")
            assert "BeeCode only touches files inside the folder" in message
            assert not isinstance(app.screen, EditorScreen), "no screen may be pushed"
            assert outside.read_bytes() == "ключ\r\n".encode("utf-8")
            # A link inside the project that points out of it is the same refusal,
            # because the policy decides from realpath and not from the spelling.
            link = project / "link.txt"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                return                                      # Windows without privileges
            message = editor.open_editor("link.txt", workdir=str(project))
            assert "OUTSIDE" in message
            assert outside.read_bytes() == "ключ\r\n".encode("utf-8")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 4 --

def test_a_file_that_cannot_be_read_is_reported_not_opened_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        (tmp_path / "binary.bin").write_bytes(b"\xff\xfe\x00\x01 \x81cp1251")
        (tmp_path / "folder").mkdir()
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            message = editor.open_editor("binary.bin", workdir=str(tmp_path))
            await pilot.pause()
            assert "could not be read" in message
            assert "not opened" in message
            assert not isinstance(app.screen, EditorScreen)
            assert "directory" in editor.open_editor("folder", workdir=str(tmp_path))
            assert "no file" in editor.open_editor("nope.txt", workdir=str(tmp_path))
            assert not isinstance(app.screen, EditorScreen)
            assert "usage" in editor.open_editor("", workdir=str(tmp_path))
            assert not isinstance(app.screen, EditorScreen)
        assert (tmp_path / "binary.bin").read_bytes() == b"\xff\xfe\x00\x01 \x81cp1251"

    asyncio.run(scenario())


def test_without_a_textual_app_the_command_only_answers_with_words(tmp_path,
                                                                  monkeypatch):
    """The classic REPL has no screen to push: the plugin must say that and stop."""
    monkeypatch.chdir(tmp_path)
    write(tmp_path / "notes.txt", BODY)
    message = editor.open_editor("notes.txt", workdir=str(tmp_path))
    assert "full-screen" in message
    assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")


# --------------------------------------------------------------------------- 5 --

def _big_log(path: Path, lines: int = 200_000) -> int:
    """A log-shaped file: real bytes, one write, no fixture that pretends."""
    chunk = "".join(f"строка {i} — the quick brown bee\r\n" for i in range(1000))
    with open(str(path), "wb") as handle:
        for _ in range(lines // 1000):
            handle.write(chunk.encode("utf-8"))
    return path.stat().st_size


def test_a_huge_file_is_capped_and_the_cap_is_announced(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    size = _big_log(tmp_path / "big.log")

    async def scenario():
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            started = time.perf_counter()
            assert editor.open_editor("big.log", workdir=str(tmp_path)) == ""
            await pilot.pause()
            took = time.perf_counter() - started
            screen = app.screen
            area = screen.query_one("#ed-area")
            assert screen.loaded.size == size
            assert screen.loaded.truncated and screen.loaded.tail_offset is not None
            assert area.document.line_count == editor.MAX_LINES + 1
            assert 0 < took < 5.0, f"a capped window took {took:.2f}s to open"
            status = screen.status_text()
            assert str(size) in status, "the file's real size has to be in the note"
            assert "read tool" in status and "outside this window" in status
            assert str(screen.loaded.lines) in status
            # One text area is the viewport: no widget was built per line.
            assert sum(1 for w in screen.walk_children()
                       if isinstance(w, editor.HiveTextArea)) == 1
            await pilot.press("end")
            assert area.cursor_location == (0, 30), "end means the end of its own line"
            await pilot.press("ctrl+k", "1", "0", "0", "enter")
            await pilot.pause()
            assert area.cursor_location[0] == area.document.line_count - 1, (
                "the last line of the capped window has to be reachable")
            assert "100%" in screen.status
            await pilot.press("ctrl+k", "7", "5", "enter")
            assert "75%" in screen.status
            TIMINGS["open"] = took
    asyncio.run(scenario())
    # The measured number the brief asks for. It belongs in the report, not in an
    # assertion: a loaded machine must not fail because this one printed a float.
    print("\nedit-ui open of "
          f"{size / 1e6:.1f} MB / 200000 lines: {TIMINGS['open']:.3f}s")


def test_saving_a_capped_file_copies_the_unloaded_tail_through(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _big_log(tmp_path / "big.log", 6000)
    original = (tmp_path / "big.log").read_bytes()

    async def scenario():
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("big.log", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            assert screen.loaded.tail_bytes > 0
            await pilot.press("ctrl+g", "1", "enter")
            await pilot.press("X")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 1
            tail_from = screen.loaded.tail_offset
        now = (tmp_path / "big.log").read_bytes()
        assert now.endswith(original[tail_from:]), "the untouched tail must survive"
        assert now.count(b"\r\n") == original.count(b"\r\n")
        assert now.startswith(b"X"), "the edit landed"
        assert not now.startswith(original[:20])

    asyncio.run(scenario())


def test_a_tail_too_big_to_splice_is_refused_instead_of_rewritten(tmp_path,
                                                                 monkeypatch):
    monkeypatch.chdir(tmp_path)
    _big_log(tmp_path / "big.log", 6000)
    original = (tmp_path / "big.log").read_bytes()
    monkeypatch.setattr(editor, "MAX_TAIL_BYTES", 16)

    async def scenario():
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("big.log", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#ed-area").insert("X")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 0
            assert "nothing was written" in screen.status
            assert screen.dirty, "the typing must stay where the user can see it"
        assert (tmp_path / "big.log").read_bytes() == original

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 6 --

def test_the_journal_is_asked_exactly_once_per_save(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from beeagent.core import journal

    calls = []

    def fake_record(workdir, tool, path, *, action="modify"):
        calls.append((workdir, tool, str(path), action))
        return {"seq": len(calls), "action": action}

    monkeypatch.setattr(journal, "record", fake_record)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            area = screen.query_one("#ed-area")
            assert calls == [], "opening a file is not a change"
            area.move_cursor((0, 0))
            area.insert("A")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(calls) == 1, calls
            workdir, tool, path, action = calls[0]
            assert tool == "edit-ui" and action == "modify"
            assert path.replace("\\", "/").endswith("notes.txt")
            assert Path(workdir) == tmp_path
            await pilot.press("ctrl+s")                       # nothing typed since
            await pilot.pause()
            assert len(calls) == 1
            area.move_cursor((0, 0))
            area.insert("B")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert len(calls) == 2, calls
            assert "saved" in screen.status
            assert screen.written_bytes == (tmp_path / "notes.txt").stat().st_size
        body = (tmp_path / "notes.txt").read_bytes()
        assert body.startswith("BAзаголовок\r\n".encode("utf-8"))
        assert body.endswith("четвёртая\r\n".encode("utf-8"))
        assert body.count(b"\r\n") == 4

    asyncio.run(scenario())


def test_a_journal_refusal_stops_the_write_and_says_so(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    from beeagent.core import journal

    def refuse(workdir, tool, path, *, action="modify"):
        raise journal.JournalError("the undo folder is not writable in this test")

    monkeypatch.setattr(journal, "record", refuse)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            area = screen.query_one("#ed-area")
            area.insert("dangerous")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 0
            assert "refused" in screen.status
            assert area.text.startswith("dangerous")
        assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")

    asyncio.run(scenario())


def test_no_journal_at_all_means_no_write(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(editor, "journal_module", lambda: None)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#ed-area").insert("x")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 0
            assert "undo journal" in screen.status
        assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")

    asyncio.run(scenario())


def test_a_file_that_changed_on_disk_after_it_was_opened_is_not_overwritten(tmp_path,
                                                                           monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            screen.query_one("#ed-area").insert(" mine")
            # Somebody else writes while the human is looking — the agent, another
            # window, a checkout. This window is now stale and must know it.
            time.sleep(0.02)
            write(tmp_path / "notes.txt", "ихняя версия\r\n")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 0
            assert "changed on disk" in screen.status
        assert (tmp_path / "notes.txt").read_bytes() == "ихняя версия\r\n".encode("utf-8")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 7 --

def test_keystrokes_the_editor_does_not_own_leave_the_buffer_alone(tmp_path,
                                                                  monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            area = screen.query_one("#ed-area")
            before = area.text
            for key in ("f1", "f2", "f5", "f9", "insert", "shift+tab", "ctrl+pageup",
                        "ctrl+pagedown", "alt+x", "super+a", "caps_lock"):
                await pilot.press(key)
                await pilot.pause()
            assert area.text == before, "an unowned key must not type"
            assert not screen.dirty
            assert type(app.screen) is EditorScreen, "an unowned key must not close"
            assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")

            # n and N belong to the editor only while a search is armed. Before a
            # search they are letters:
            await pilot.press(*list("nN"))
            assert area.text == "nN" + before
            area.delete((0, 0), (0, 2))
            assert area.text == before

            await pilot.press("ctrl+f")
            await pilot.press(*list("за"))
            await pilot.press("enter")
            armed_text = area.text
            assert screen.search_armed
            await pilot.press("n", "N", "n", "N")
            assert area.text == armed_text, "n/N may not type into the file"
            await pilot.press("q")                     # every other key still types
            assert area.text == "q" + armed_text
            await pilot.press("ctrl+f")                # the prompt, open this time
            await pilot.pause()
            assert screen.prompt_mode == "find"
            await pilot.press("escape")                # escape leaves the prompt only
            await pilot.pause()
            assert screen.prompt_mode == "", "escape must not close the file"
            assert type(app.screen) is EditorScreen
            assert area.text == "q" + armed_text, "and it must not touch the buffer"
            await pilot.press("escape")                # and a dirty file asks first
            await pilot.pause()
            assert type(app.screen).__name__ == "DiscardScreen"
            await pilot.press("d")
            await pilot.pause()
        assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 8 --

def test_line_endings_the_editor_cannot_rebuild_refuse_to_save(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    page_break = "первая\x0cвторая\r\nтретья\r\n"

    async def scenario():
        write(tmp_path / "kernel.c", page_break)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("kernel.c", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            assert screen.can_save is False
            assert screen.loaded.structural_breaks
            assert "Ctrl+S is refused" in screen.status_text()
            screen.query_one("#ed-area").insert("X")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 0
            assert "refused" in screen.status
            assert screen.dirty, "the typing is kept; only the write is refused"
        assert (tmp_path / "kernel.c").read_bytes() == page_break.encode("utf-8")

    asyncio.run(scenario())


def test_mixed_line_endings_are_announced_and_still_saveable(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "mixed.txt", "a\r\nb\nc\r\n")
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("mixed.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            assert screen.loaded.mixed_newlines and screen.can_save
            assert "mixes CRLF and LF" in screen.status_text()
            screen.query_one("#ed-area").insert("!")
            await pilot.press("ctrl+s")
            await pilot.pause()
            assert screen.saves == 1
        # The edit landed, and the file's one LF line came back as CRLF: that is
        # the normalisation the status line warned about, not a surprise.
        assert (tmp_path / "mixed.txt").read_bytes() == b"!a\r\nb\r\nc\r\n"

    asyncio.run(scenario())


# --------------------------------------------------------------------------- 9 --

# Rich hands a style back as `rgb(r, g, b)`, not as the hex the palette is
# written in, so the colours a screen paints are compared as values.
_RGB_IN_STYLE = re.compile(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)")


def _triplet(hex_colour: str) -> tuple:
    body = hex_colour.lstrip("#")
    return tuple(int(body[i:i + 2], 16) for i in (0, 2, 4))


def _painted_colours(styles: str) -> list:
    return [tuple(int(g) for g in match) for match in _RGB_IN_STYLE.findall(styles)]


def _hue(colour: tuple) -> float:
    """0..360, the way a colour wheel reads it: honey ~46, leaf ~89."""
    high, low = max(colour), min(colour)
    if high == low:
        return -1.0
    span = float(high - low)
    red, green, blue = colour
    if high == red:
        return (60.0 * ((green - blue) / span)) % 360.0
    if high == green:
        return 60.0 * (2.0 + (blue - red) / span)
    return 60.0 * (4.0 + (red - green) / span)


def test_the_screen_is_painted_in_honey_and_leaf_not_in_textual_defaults(tmp_path,
                                                                        monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            screen = app.screen
            for colour in (HONEY, LEAF, DARK_LEAF, HIVE_BACKDROP, HIVE_PANEL):
                assert colour in screen.CSS, colour
            assert "cyan" not in screen.CSS and "blue" not in screen.CSS
            area = screen.query_one("#ed-area")
            assert area.styles.background.hex == HIVE_PANEL.upper()
            # Focused, so the leaf focus frame is what the user actually sees; the
            # resting DARK_LEAF border is asserted through the sheet above.
            assert area.styles.border_top[1].hex == LEAF.upper()
            assert area.styles.scrollbar_color.hex == HONEY.upper()
            title = screen.query_one("#ed-title").render()
            styles = " ".join(str(span.style) for span in title.spans)
            painted = _painted_colours(styles)
            assert painted, "the title is painted at all"
            bases = [_triplet(HONEY), _triplet(LEAF), _triplet(DARK_LEAF)]
            arc = (min(_hue(base) for base in bases) - 15.0,
                   max(_hue(base) for base in bases) + 25.0)
            off_brand = [colour for colour in painted
                         if not (max(colour) - min(colour) >= 40
                                 and arc[0] <= _hue(colour) <= arc[1])]
            assert not off_brand, (
                f"the title is painted outside the honey-to-leaf arc {arc}: "
                f"{off_brand} — Textual's own defaults sit near 180 and 240")
            assert "cyan" not in styles and "blue" not in styles
            assert screen.query_one("#ed-keys").render is not None
            assert "Ctrl+S" in screen.query_one("#ed-keys").render().plain

    asyncio.run(scenario())


# -------------------------------------------------------------------------- 10 --

def _install_pack(project: Path, name: str = "edit-ui") -> None:
    """The way `test_shipped_plugins.py` does it: copy the real template in."""
    shutil.copytree(TEMPLATES / name, project / ".beeagent" / "plugins" / name)
    state = project / ".beeagent" / "plugins.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(json.dumps({"installed": {name: {"type": "plugin",
                                                      "enabled": True}}}),
                     encoding="utf-8")


def test_the_pack_absent_changes_nothing_at_all(tmp_path):
    """Nothing about BeeCode moves until this pack is installed.

    Run in a child interpreter: in this one the editor was already imported by
    the tests above, so `sys.modules` here would prove nothing.
    """
    probe = (
        "import sys, os, json;"
        f"sys.path.insert(0, {str(REPO_ROOT)!r});"
        "from beeagent.ui import commands, tui, components, skin;"
        "from beeagent.tools import edit;"
        f"os.chdir({str(tmp_path)!r});"
        "from beeagent.config.schema import BeeConfig;"
        "from beeagent.core.agent import Agent;"
        f"a = Agent(config=BeeConfig(), workdir={str(tmp_path)!r});"
        "print(json.dumps({"
        "'editor_loaded': 'beeagent.ui.editor' in sys.modules,"
        "'command': [c.name for c in commands.COMMANDS].count('edit-ui'),"
        "'edit_tools': sorted(a.tools.list_names()).count('edit'),"
        "'edit_handler': type(a.tools.get('edit')).__name__,"
        "'errors': a.plugins.load_errors,"
        "'withheld': a.plugins.withheld,"
        "'plugins': len(a.plugins.manager.installed_plugin_dirs())}))"
    )
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          encoding="utf-8", timeout=300)
    assert done.returncode == 0, done.stderr
    report = json.loads(done.stdout.strip().splitlines()[-1])
    assert report["plugins"] == 0, "the test project has nothing installed"
    assert report["editor_loaded"] is False, "the editor is this plugin's module alone"
    assert report["command"] == 0
    assert report["edit_tools"] == 1 and report["edit_handler"] == "EditTool"
    assert report["errors"] == [] and report["withheld"] == []


def test_the_command_appears_once_when_installed_and_vanishes_when_not(tmp_path,
                                                                      monkeypatch):
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.ui.commands import COMMANDS, HANDLERS, ReplContext, dispatch

    monkeypatch.chdir(tmp_path)
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    baseline = [(c.name, c.description, c.usage, c.category) for c in COMMANDS]
    edit_tool = agent.tools.get("edit")
    assert edit_tool is not None
    assert "edit-ui" not in [c.name for c in COMMANDS]

    _install_pack(tmp_path)
    assert agent.reload_extensions() == []
    assert [c.name for c in COMMANDS].count("edit-ui") == 1
    contributed = agent.plugins.extensions.by_plugin("edit-ui")
    kinds = [c.kind for c in contributed]
    # One command and the two caps it honours, and nothing that reaches the model:
    # no tool, no hook, so the agent's own `edit` stays the only way in.
    assert sorted(kinds) == ["command", "setting", "setting"], kinds
    assert [c.name for c in contributed if c.kind == "command"] == ["edit-ui"]
    assert sorted(c.name for c in contributed if c.kind == "setting") == [
        "edit-ui.max_bytes", "edit-ui.max_lines"]
    assert not [c for c in contributed if c.kind in ("tool", "hook")], kinds
    assert "edit-ui" in HANDLERS
    assert agent.reload_extensions() == []                  # no doubled command
    assert [c.name for c in COMMANDS].count("edit-ui") == 1

    # The agent's own edit tool is the same object with the same schema.
    assert agent.tools.get("edit") is edit_tool
    assert agent.tools.get("edit").to_schema() == edit_tool.to_schema()

    write(tmp_path / "notes.txt", BODY)
    ctx = lambda: ReplContext(agent=agent, config=agent.config, session=Session())
    assert "full-screen" in _plain(dispatch(ctx(), "/edit-ui notes.txt").output)
    assert (tmp_path / "notes.txt").read_bytes() == BODY.encode("utf-8")
    assert "/edit-ui <path>" in _plain(dispatch(ctx(), "/edit-ui").output)
    assert any(c.usage == "/edit-ui <file>" for c in COMMANDS if c.name == "edit-ui")

    shutil.rmtree(tmp_path / ".beeagent" / "plugins" / "edit-ui")
    (tmp_path / ".beeagent" / "plugins.json").write_text('{"installed": {}}',
                                                        encoding="utf-8")
    assert agent.reload_extensions() == []
    assert [(c.name, c.description, c.usage, c.category) for c in COMMANDS] == baseline
    assert "edit-ui" not in HANDLERS
    assert _plain(dispatch(ctx(), "/edit-ui notes.txt").output).startswith("Unknown")


def test_the_shipped_pack_is_valid_without_an_agent_and_survives_a_bad_arg(tmp_path,
                                                                          monkeypatch):
    """A plugin bug must not take the REPL down with it."""
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext, dispatch

    monkeypatch.chdir(tmp_path)
    _install_pack(tmp_path)
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    assert agent.plugins.load_errors == []
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    for line in ("/edit-ui", "/edit-ui ", '/edit-ui "missing file.txt"',
                 "/edit-ui ../elsewhere/x.txt", "/edit-ui ."):
        result = dispatch(ctx, line)
        assert result.output is not None, line
        assert not hasattr(result, "error") or result.error is None
    assert agent.plugins.load_errors == []


# -------------------------------------------------------------------------- 11 --

def test_the_limits_are_settings_a_user_can_override(tmp_path, monkeypatch):
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.ext.api import ExtensionAPI

    monkeypatch.chdir(tmp_path)
    config = BeeConfig()
    config.extensions = {"edit-ui": {"max_lines": 7, "max_bytes": 4096}}
    _install_pack(tmp_path)
    agent = Agent(config=config, workdir=str(tmp_path))
    assert agent.plugins.load_errors == []
    api = ExtensionAPI("edit-ui", agent.plugins.extensions, agent=agent, config=config)
    assert api.get("max_lines") == 7 and api.get("max_bytes") == 4096

    write(tmp_path / "notes.txt", "\n".join(f"line {i}" for i in range(50)) + "\n")
    loaded = editor.load_file(tmp_path / "notes.txt", max_lines=int(api.get("max_lines")),
                              max_bytes=int(api.get("max_bytes")))
    assert loaded.lines == 7 and loaded.truncated and loaded.tail_offset is not None


def test_load_file_never_reads_more_than_the_cap_and_counts_the_window(tmp_path,
                                                                      monkeypatch):
    monkeypatch.chdir(tmp_path)
    small = write(tmp_path / "small.txt", "one\ntwo\n")
    loaded = editor.load_file(small)
    assert not loaded.truncated and loaded.tail_offset is None
    assert loaded.text == "one\ntwo\n" and loaded.lines == 2
    assert loaded.can_save and not loaded.mixed_newlines

    empty = write(tmp_path / "empty.txt", "")
    loaded = editor.load_file(empty)
    assert loaded.text == "" and loaded.lines == 0 and loaded.can_save

    no_final_newline = write(tmp_path / "tail.txt", "one\ntwo")
    loaded = editor.load_file(no_final_newline)
    assert loaded.text == "one\ntwo" and loaded.can_save

    windowed = write(tmp_path / "wide.txt", "one\n" + "а" * 200 + "\n")
    loaded = editor.load_file(windowed, max_lines=5, max_bytes=8)
    assert loaded.truncated and loaded.tail_offset is not None
    # A cap the file crossed at least once is cut at that boundary: the window is
    # whole lines and carries the terminator it stopped on.
    assert "\ufffd" not in loaded.text
    assert loaded.text == "one\n" and loaded.tail_bytes == windowed.stat().st_size - 4

    # A cap inside the very first line has no boundary to stop at, so the window
    # is that partial line and the tail begins in the middle of it — which is how
    # `test_saving_a_capped_file_copies_the_unloaded_tail_through` puts them back.
    first_line = write(tmp_path / "one_long_line.txt", "а" * 200 + "\n")
    loaded = editor.load_file(first_line, max_lines=1, max_bytes=41)
    assert loaded.truncated and loaded.tail_offset is not None
    assert "\ufffd" not in loaded.text, "a cap inside a Cyrillic character drops the byte"
    assert loaded.text == "а" * 20 and loaded.tail_offset == 40
    assert loaded.tail_bytes == first_line.stat().st_size - 40


def test_a_saved_file_can_be_reopened_and_shows_no_dirty_flag(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def scenario():
        write(tmp_path / "notes.txt", BODY)
        app = HostApp()
        async with app.run_test(size=(120, 40)) as pilot:
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            first = app.screen
            first.query_one("#ed-area").insert("Z")
            await pilot.press("ctrl+s")
            await pilot.pause()
            await pilot.press("ctrl+q")
            await pilot.pause()
            editor.open_editor("notes.txt", workdir=str(tmp_path))
            await pilot.pause()
            second = app.screen
            assert isinstance(second, EditorScreen)
            assert not second.dirty
            assert "Z" in second.query_one("#ed-area").text
            assert second.query_one("#ed-title").render().plain == second.title_text()
            await pilot.press("ctrl+q")
            await pilot.pause()
            assert app.screen is not second, "a clean file closes without an interview"
        assert (tmp_path / "notes.txt").read_bytes() == ("Z" + BODY).encode("utf-8")

    asyncio.run(scenario())


# ------------------------------------------------------------------------ help --

def _plain(renderable) -> str:
    import io

    from rich.console import Console

    if isinstance(renderable, str):
        return renderable
    console = Console(file=io.StringIO(), width=110, force_terminal=False)
    console.print(renderable)
    return console.file.getvalue()
