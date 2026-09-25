"""Results that are not the truth are the worst class of bug here.

Three tools were measured announcing things that had not happened:

* `write` announced a character count as a byte count ("Written 13 bytes" for a
  25-byte Cyrillic file), and `os.replace` let it delete a symlink and leave the
  real file stale;
* `todo` called a torn plan "No tasks" and then saved a fresh list over the user's
  bytes;
* `web_search` fed an HTTP 202 refusal to the parser and answered "No results
  found" with `error=False`.

Every test below checks a real filesystem effect or a real tool result, in a temp
directory, with no network: `httpx` is stubbed and a live request is a failure.
"""
import json
import os
import pathlib
import re

import httpx
import pytest

from beeagent import i18n
from beeagent.tools import base as base_mod
from beeagent.tools import web_search as web_search_mod
from beeagent.tools.base import read_text_preserving, write_text_preserving
from beeagent.tools.todo import TODO_FILE, TodoTool


@pytest.fixture(autouse=True)
def _english_and_a_scratch_dir(tmp_path):
    """Pin the language and the cwd, and hand both back.

    The messages are bilingual pairs, and todo.json is a relative path, so an
    earlier module's `/lang` test or working directory must not decide what these
    assertions see.  The cwd is restored here rather than through `monkeypatch`
    because `tests/conftest.py` checks it before that fixture's undo runs.
    """
    before = os.getcwd()
    os.chdir(str(tmp_path))
    previous_lang = i18n.get_lang()
    i18n.set_lang("en")
    try:
        yield tmp_path
    finally:
        os.chdir(before)
        i18n.set_lang(previous_lang)


def _symlink(link: pathlib.Path, target: str) -> bool:
    """Make a symlink, or say this platform will not let us check it."""
    try:
        os.symlink(target, str(link))
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"this checkout cannot create symlinks: {exc}")
    return True


# --- 1. the size a tool announces has to be the size the file has ----------

def test_the_announced_size_is_the_bytes_on_disk(tmp_path):
    """A model reasons about how much it wrote; the number must be real.

    Text-mode `handle.write()` returns characters, and 13 characters of Russian
    are 25 bytes on disk — the old code handed that 13 back as a byte count.
    """
    text = "Привет, мир!\nЕщё строка.\n"          # 25 characters
    target = tmp_path / "cyrillic.txt"
    expected = len(text.encode("utf-8"))
    assert expected > len(text), "the point of the file is that it is multi-byte"

    reported = write_text_preserving(target, text)

    assert reported == expected == target.stat().st_size, (
        f"announced {reported}, the file is {target.stat().st_size}")
    assert target.read_bytes().decode("utf-8") == text


def test_line_endings_survive_the_rewrite(tmp_path):
    """`newline=""` on read, binary on write: no translation in either direction."""
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"first\r\nsecond\r\n")
    content = read_text_preserving(target)
    assert "\r\n" in content, "read must not invent LF"

    write_text_preserving(target, content + "third\r\n")

    assert target.read_bytes() == b"first\r\nsecond\r\nthird\r\n"


def test_a_symlink_stays_a_symlink_and_the_refusal_names_the_real_path(tmp_path):
    """The decision: refuse, and point at the real file.

    `os.replace` renames over the link itself, which is how "write config.json"
    used to delete a user's symlink into a shared directory, leave a plain file in
    its place and keep the shared file on the old bytes — all announced as a clean
    write. Following the link instead would let a project-local name rewrite a
    file outside the permission check, so the tool now stops.
    """
    shared = tmp_path / "shared.json"
    shared.write_text('{"keep": "original"}\n', encoding="utf-8")
    link = tmp_path / "config.json"
    _symlink(link, str(shared))

    with pytest.raises(OSError) as caught:
        write_text_preserving(link, '{"keep": "rewritten"}\n')

    message = str(caught.value)
    assert str(shared.resolve()) in message, f"the real path must be named: {message}"
    assert os.path.islink(str(link)), "the link is still a link"
    assert shared.read_text(encoding="utf-8") == '{"keep": "original"}\n', (
        "nothing was written through it")
    assert not list(tmp_path.glob("*.beecode-tmp")), "and no temp file was left"


def test_a_dangling_symlink_is_refused_too(tmp_path):
    """A link to a file that is not there yet is still a link to destroy."""
    link = tmp_path / "points.nowhere"
    _symlink(link, str(tmp_path / "nowhere.txt"))

    with pytest.raises(OSError):
        write_text_preserving(link, "content")

    assert os.path.islink(str(link))
    assert not (tmp_path / "nowhere.txt").exists()


def test_a_short_write_is_never_renamed_onto_the_target(tmp_path, monkeypatch):
    """A full disk can report fewer bytes than it was given; the target wins."""
    target = tmp_path / "keep.txt"
    target.write_text("старое содержимое", encoding="utf-8")
    before = target.read_bytes()
    real_getsize = os.path.getsize

    def truncated(path):
        return real_getsize(path) - 5

    monkeypatch.setattr(base_mod.os.path, "getsize", truncated)
    with pytest.raises(OSError) as caught:
        write_text_preserving(target, "новое содержимое")
    assert "incomplete write" in str(caught.value)

    assert target.read_bytes() == before
    assert not list(tmp_path.glob("*.beecode-tmp"))


def test_an_unencodable_string_touches_nothing_at_all(tmp_path):
    """Encoding first: the old text-mode write failed halfway and left half a file."""
    target = tmp_path / "half.txt"
    target.write_text("важное", encoding="utf-8")

    with pytest.raises(UnicodeEncodeError):
        write_text_preserving(target, "x\ud800y")

    assert target.read_text(encoding="utf-8") == "важное"
    assert not list(tmp_path.glob("*.beecode-tmp")), "no temp file was even created"


def test_a_write_that_cannot_open_leaves_the_original_alone(tmp_path, monkeypatch):
    """The atomic guarantee the helper already had, pinned beside the new ones."""
    target = tmp_path / "orig.txt"
    target.write_text("original", encoding="utf-8")

    def explode(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("builtins.open", explode)
    with pytest.raises(OSError):
        write_text_preserving(target, "replacement")
    assert target.read_text(encoding="utf-8") == "original"


# --- 2. a plan the tool cannot read is not a plan the tool may replace -----

def _todo_file() -> pathlib.Path:
    return pathlib.Path(TODO_FILE)


def _write_raw(text: str) -> None:
    path = _todo_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_torn_plan_is_not_reported_as_no_tasks():
    """The audit's chain: half a file -> "No tasks" -> the next save wins."""
    _write_raw('[{"id": 1, "text": "fix the parser", "done": false}, {"id": 2,')
    tool = TodoTool()

    result = tool.execute("list")

    assert result.error is True, "an unreadable plan is not an empty one"
    assert "No tasks" not in result.output
    assert "not valid JSON" in result.output, result.output
    assert _todo_file().read_text(encoding="utf-8").startswith('[{"id": 1'), (
        "listing must not touch the bytes")


def test_no_write_action_saves_over_a_plan_it_could_not_read():
    """`add` used to be the moment the user's plan disappeared."""
    torn = '[{"id": 1, "text": "keep this plan", "done": false}, {"id": 2,'
    _write_raw(torn)
    tool = TodoTool()

    for result in (tool.execute("add", text="a new task"),
                   tool.execute("done", id=1),
                   tool.execute("remove", id=1)):
        assert result.error is True, result.output
        assert "Nothing was changed" in result.output, result.output
        assert TODO_FILE in result.output, "and it says which file to fix"

    assert _todo_file().read_text(encoding="utf-8") == torn


def test_a_list_of_the_wrong_shape_is_refused_as_well():
    _write_raw('{"note": "the user wrote this by hand"}')
    result = TodoTool().execute("add", text="a task")

    assert result.error is True
    assert "not a list of tasks" in result.output
    assert json.loads(_todo_file().read_text(encoding="utf-8"))["note"] == (
        "the user wrote this by hand")


def test_rows_that_are_not_task_records_block_a_save_but_still_show():
    """A partly readable file is still a file whose rest we would overwrite."""
    _write_raw('["read the failing test", {"id": 2, "text": "fix it", "done": false}]')
    tool = TodoTool()

    listed = tool.execute("list")
    assert listed.error is True, "one bad row must not read as a clean list"
    assert "fix it" in listed.output, "what does read is still shown"

    refused = tool.execute("add", text="third task")
    assert refused.error is True and "not task records" in refused.output
    assert _todo_file().read_text(encoding="utf-8").startswith("[")


def test_a_missing_file_is_a_honest_empty_plan_and_can_be_written():
    tool = TodoTool()
    assert tool.execute("list").output == "No tasks"

    added = tool.execute("add", text="write the regression test")

    assert added.error is False, added.output
    assert "1 in the plan" in added.output and _todo_file().exists()


def test_a_saved_plan_reports_the_bytes_the_file_actually_has():
    tool = TodoTool()

    for step in (lambda: tool.execute("add", text="проверить план"),
                 lambda: tool.execute("done", id=1)):
        result = step()
        assert result.error is False, result.output
        announced = int(re.search(r"(\d+) bytes written", result.output).group(1))
        assert announced == _todo_file().stat().st_size, (
            f"announced {announced}, the file is {_todo_file().stat().st_size}")
        assert announced == result.metadata["bytes"]
    assert json.loads(_todo_file().read_text(encoding="utf-8"))[0]["done"] is True


def test_a_save_that_fails_is_reported_as_a_failure_and_keeps_the_old_plan(
        tmp_path, monkeypatch):
    tool = TodoTool()
    tool.execute("add", text="the plan that survives")
    before = _todo_file().read_bytes()
    real_replace = os.replace

    def explode(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(base_mod.os, "replace", explode)
    result = tool.execute("add", text="a task that never lands")

    assert result.error is True, "an add that did not reach the disk is not an add"
    assert "NOT saved" in result.output
    assert _todo_file().read_bytes() == before
    monkeypatch.setattr(base_mod.os, "replace", real_replace)
    assert not list(tmp_path.glob("*.beecode-tmp"))


def test_a_todo_save_refuses_to_become_a_symlink_replacement(tmp_path, monkeypatch):
    """Same helper, same rule, now on the plan file itself."""
    real = tmp_path / "plan-of-the-team.json"
    real.write_text('[{"id": 7, "text": "shared plan", "done": false}]', encoding="utf-8")
    _todo_file().parent.mkdir(parents=True, exist_ok=True)
    _symlink(_todo_file(), str(real))

    result = TodoTool().execute("add", text="mine")

    assert result.error is True, "the refusal must not look like a save"
    assert str(real.resolve()) in result.output
    assert json.loads(real.read_text(encoding="utf-8"))[0]["text"] == "shared plan"


# --- 3. a search that was refused is not a search that found nothing ______

class _Stub:
    """Stands in for httpx: a live request in this suite is a bug, not a slow test."""

    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def _response(status: int, text: str = "") -> httpx.Response:
    return httpx.Response(status_code=status, text=text,
                          request=httpx.Request("GET", web_search_mod.ENDPOINT))


def _page(titles, start=1):
    rows = []
    for offset, title in enumerate(titles):
        number = start + offset
        href = f"//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2F{number}&rut=x"
        rows.append(f'<h2 class="result__title"><a class="result__a" '
                    f'href="{href}">{title} <b>{number}</b></a></h2>'
                    f'<a class="result__snippet" href="ignore me">snippet {number}</a>')
    return "<html><body>" + "".join(rows) + "</body></html>"


def _search(monkeypatch, response=None, error=None, query="python tutorial"):
    stub = _Stub(response=response, error=error)
    monkeypatch.setattr(web_search_mod, "httpx", stub)
    result = web_search_mod.WebSearchTool().execute(query=query)
    return result, stub


def test_an_http_refusal_is_not_reported_as_an_empty_web(monkeypatch):
    """Measured: every audit query drew HTTP 202 and the tool said "No results found"."""
    result, _ = _search(monkeypatch, _response(202, ""))

    assert result.error is True, "a refusal has to fail loudly"
    assert "202" in result.output, result.output
    assert "No results found" not in result.output
    assert "not an empty web" in result.output, result.output
    assert result.metadata["status"] == 202
    assert result.metadata["results"] == 0


def test_a_redirect_or_a_server_error_says_so(monkeypatch):
    for status in (301, 403, 429, 500):
        result, _ = _search(monkeypatch, _response(status, "page bytes"))
        assert result.error is True, status
        assert str(status) in result.output, result.output


def test_the_status_is_checked_before_any_result_is_read(monkeypatch):
    """A 200-shaped refusal with links in it must still be a refusal."""
    result, _ = _search(monkeypatch, _response(503, _page(["a title"])))

    assert result.error is True and "1." not in result.output


def test_results_come_back_with_their_urls(monkeypatch):
    result, stub = _search(monkeypatch, _response(200, _page(["First & best", "Second"])))

    assert result.error is False, result.output
    lines = result.output.splitlines()
    assert lines[0] == "1. First & best 1"
    assert lines[1] == "   https://example.com/1", "the wrapper is unwrapped"
    assert lines[2] == "2. Second 2"
    assert lines[3] == "   https://example.com/2"
    assert "snippet" not in result.output, "only result links are results"
    assert stub.calls[0][0] == web_search_mod.ENDPOINT


def test_results_that_are_not_shown_are_counted_out_loud(monkeypatch):
    page = _page([f"title {i}" for i in range(1, 14)])      # 13 results on the page
    result, _ = _search(monkeypatch, _response(200, page))

    numbered = [line for line in result.output.splitlines()
                if re.match(r"^\d+\. ", line)]
    assert len(numbered) == web_search_mod.LIMIT, numbered
    assert "13." not in result.output
    assert "3 more result(s) on the page were dropped" in result.output, result.output
    assert result.metadata["dropped"] == 3 and result.metadata["found"] == 13


def test_a_result_without_a_url_says_it_has_none(monkeypatch):
    page = ('<html><h2 class="result__title"><a class="result__a" href="">'
            'titleless</a></h2></html>')
    result, _ = _search(monkeypatch, _response(200, page))

    assert "(the page gave no URL)" in result.output, result.output
    assert "came back without a URL" in result.output


def test_a_200_page_with_no_links_names_both_possibilities(monkeypatch):
    result, _ = _search(monkeypatch, _response(200, "<html><body>nothing here</body></html>"))

    assert "No results found" not in result.output, "the old flat denial"
    assert "no result links" in result.output and "not a results page" in result.output
    assert result.metadata["status"] == 200


def test_a_transport_failure_is_reported_as_one(monkeypatch):
    result, _ = _search(monkeypatch, error=httpx.ConnectError("no route to host"))

    assert result.error is True
    assert "no route to host" in result.output


def test_an_empty_query_is_not_sent(monkeypatch):
    result, stub = _search(monkeypatch, _response(200, _page(["x"])), query="   ")

    assert result.error is True and stub.calls == [], "nothing was searched"


def test_the_new_messages_are_bilingual(monkeypatch):
    """Rule of this project: a user-facing string ships in both languages."""
    result, _ = _search(monkeypatch, _response(202, ""))
    assert "not an empty web" in result.output

    i18n.set_lang("ru")
    try:
        refused, _ = _search(monkeypatch, _response(202, "страница"))
        _write_raw("{ это не json")
        torn = TodoTool().execute("list")
        saved = TodoTool().execute("add", text="новый пункт")
    finally:
        i18n.set_lang("en")

    assert "поисковый сервер отклонил запрос" in refused.output, refused.output
    assert "не корректный JSON" in torn.output, torn.output
    assert saved.error is True and "Ничего не изменено" in saved.output, saved.output
