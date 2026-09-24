"""The four plugins that answer last week's pool failures.

`seat`, `trace`, `scoreboard` and `compact` are built the way the shipped ones
are: a template directory, `plugin.json`, `setup(api)`. They are loaded here from
the real templates into a throwaway project and driven through the same dispatch
the terminal uses. Nothing in this file reaches the network — the pool's
`/v1/seat` call is replaced by a recording fake, and a test that lets a real
connection through would fail on the fake's missing transport.
"""
import io
import json
import shutil
import sys
import time as real_time
from pathlib import Path

import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.ext.api import emit
from beeagent.i18n import get_lang, set_lang
from beeagent.ui import skin
from beeagent.ui.commands import COMMANDS, HANDLERS, ReplContext, dispatch

TEMPLATES = Path(__file__).resolve().parent.parent / "beeagent" / "plugins" / "templates" / "plugins"
NEW = ["seat", "trace", "scoreboard", "compact"]

SEAT_REPORT = {"requests": 141, "requests_limit": 200,
               "tokens": 90000, "tokens_limit": 400000, "resets_in_seconds": 15120}


@pytest.fixture(autouse=True)
def _clean_state():
    """Commands, skins and language are module globals; do not leak them."""
    commands_before = list(COMMANDS)
    handlers_before = dict(HANDLERS)
    language = get_lang()
    set_lang("en")
    skin.reset()
    yield
    COMMANDS[:] = commands_before
    HANDLERS.clear()
    HANDLERS.update(handlers_before)
    skin.reset()
    set_lang(language)


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def load(project, name):
    """Install a template the way `/plugin install` does, and boot an Agent on it."""
    shutil.copytree(TEMPLATES / name, project / ".beeagent" / "plugins" / name,
                    dirs_exist_ok=True)
    (project / ".beeagent").mkdir(parents=True, exist_ok=True)
    (project / ".beeagent" / "plugins.json").write_text(
        json.dumps({"installed": {name: {"type": "plugin", "enabled": True}}}),
        encoding="utf-8")
    return Agent(config=BeeConfig(), workdir=str(project))


def run(agent, line):
    return dispatch(ReplContext(agent=agent, config=agent.config, session=Session()), line)


def plain(result):
    body = result.output
    if isinstance(body, str):
        return body
    from rich.console import Console

    console = Console(file=io.StringIO(), width=120, force_terminal=False)
    console.print(body)
    return console.file.getvalue()


def module(name):
    """The loaded plugin module — the only handle a test has on its network call."""
    return sys.modules[f"beeagent_plugin_{name}"]


def stored(agent, name):
    """What a plugin left in the project's own .beeagent directory."""
    return json.loads((Path(agent.workdir) / ".beeagent" / name).read_text(encoding="utf-8"))


# --- the pool, without a network ------------------------------------------------

class PoolRefused(Exception):
    """Stands in for the httpx error a dead or sleeping pool raises."""


class FakeResponse:
    def __init__(self, status, body=None, broken=False):
        self.status_code = status
        self._body = body
        self._broken = broken

    def json(self):
        if self._broken:
            raise ValueError("body is not JSON")
        return self._body


class FakeClient:
    def __init__(self, pool, timeout=None):
        self.pool = pool
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None):
        self.pool.requests.append((url, headers, self.timeout))
        if self.pool.error is not None:
            raise self.pool.error
        return self.pool.response


class FakeHttpx:
    """A pool that answers the same thing every time, or does not answer at all."""

    HTTPError = PoolRefused

    def __init__(self, response=None, error=None):
        self.requests = []
        self.response = response
        self.error = error

    def Client(self, timeout=None):
        return FakeClient(self, timeout)


def seated(project, agent):
    agent.config.pool_url = "https://pool.invalid"
    agent.config.pool_token = "seat-token"
    return agent


# --- manifests ------------------------------------------------------------------

def test_every_new_plugin_ships_a_manifest():
    for name in NEW:
        directory = TEMPLATES / name
        manifest = json.loads((directory / "plugin.json").read_text(encoding="utf-8"))
        assert manifest["name"] == name and manifest["type"] == "plugin"
        assert (directory / manifest["entry"]).is_file()
        assert manifest["description"].strip()


def test_the_new_plugins_load_without_errors(project):
    for name in NEW:
        agent = load(project, name)
        assert not agent.plugins.load_errors, (name, agent.plugins.load_errors)


# --- seat -----------------------------------------------------------------------

def test_seat_shows_what_is_left_and_when_it_comes_back(project, monkeypatch):
    agent = seated(project, load(project, "seat"))
    calls = []

    def fake_fetch(url, token, timeout=0):
        calls.append((url, token, timeout))
        return dict(SEAT_REPORT)

    monkeypatch.setattr(module("seat"), "fetch_seat", fake_fetch)
    text = plain(run(agent, "/seat"))

    assert calls == [("https://pool.invalid", "seat-token", 12.0)]
    assert "59" in text                          # 200 - 141 requests
    assert "310,000" in text                     # 400 000 - 90 000 tokens
    assert "141" in text and "90,000" in text    # spent, but not instead of left
    assert "4h 12m" in text                      # 15120 s, spelled out
    assert "pool.invalid" in text


def test_seat_warns_when_the_budget_is_already_gone(project, monkeypatch):
    agent = seated(project, load(project, "seat"))
    spent = dict(SEAT_REPORT, requests=200, tokens=400000)
    monkeypatch.setattr(module("seat"), "fetch_seat", lambda url, token, timeout=0: spent)
    text = plain(run(agent, "/seat"))
    assert "spent" in text and "g4f" in text


def test_seat_names_the_missing_endpoint_instead_of_failing(project, monkeypatch):
    agent = seated(project, load(project, "seat"))
    fake = FakeHttpx(response=FakeResponse(404, {"error": "unknown path"}))
    monkeypatch.setattr(module("seat"), "httpx", fake)
    text = plain(run(agent, "/seat"))

    assert "/v1/seat" in text and "pool.invalid" in text
    assert fake.requests == [("https://pool.invalid/v1/seat",
                              {"Authorization": "Bearer seat-token"}, 12.0)]


def test_seat_survives_a_pool_that_is_asleep(project, monkeypatch):
    agent = seated(project, load(project, "seat"))
    monkeypatch.setattr(module("seat"), "httpx",
                        FakeHttpx(error=PoolRefused("connect timed out")))
    text = plain(run(agent, "/seat"))
    assert "PoolRefused" in text and "pool.invalid" in text
    assert "Traceback" not in text


def test_seat_refuses_to_pretty_print_a_body_that_is_not_a_seat_report(project, monkeypatch):
    """An empty body is what a broken upstream looks like; say which side broke."""
    agent = seated(project, load(project, "seat"))
    monkeypatch.setattr(module("seat"), "httpx",
                        FakeHttpx(response=FakeResponse(200, None, broken=True)))
    assert "JSON" in plain(run(agent, "/seat"))

    monkeypatch.setattr(module("seat"), "httpx",
                        FakeHttpx(response=FakeResponse(200, {"requests": 12})))
    assert "counters" in plain(run(agent, "/seat"))


def test_seat_reports_what_the_pool_said_when_it_refuses_us(project, monkeypatch):
    agent = seated(project, load(project, "seat"))
    monkeypatch.setattr(module("seat"), "httpx", FakeHttpx(
        response=FakeResponse(401, {"error": "no seat — /pool enroll first"})))
    text = plain(run(agent, "/seat"))
    assert "401" in text and "/pool enroll" in text


def test_seat_without_a_seat_never_opens_a_connection(project, monkeypatch):
    agent = load(project, "seat")
    agent.config.pool_token = ""
    fake = FakeHttpx(response=FakeResponse(200, SEAT_REPORT))
    monkeypatch.setattr(module("seat"), "httpx", fake)

    assert "/pool enroll" in plain(run(agent, "/seat"))
    agent.config.pool_url = ""
    assert "pool url" in plain(run(agent, "/seat"))
    assert fake.requests == []


def test_seat_does_not_expose_its_own_bug_as_a_traceback(project, monkeypatch):
    agent = seated(project, load(project, "seat"))

    def explode(url, token, timeout=0):
        raise ZeroDivisionError("no seat for you")

    monkeypatch.setattr(module("seat"), "fetch_seat", explode)
    text = plain(run(agent, "/seat"))
    assert "ZeroDivisionError" in text and "Traceback" not in text


def test_the_reset_is_readable_at_every_size(project):
    load(project, "seat")
    remaining = module("seat")._remaining
    assert remaining(15120) == "4h 12m"
    assert remaining(3600) == "1h"
    assert remaining(540) == "9m"
    assert remaining(45) == "45s"
    assert remaining(None) == "unknown"
    assert remaining(-60) == "0s"


# --- trace ----------------------------------------------------------------------

def fail(agent, message, model="qwen3.6", provider="pool"):
    agent.config.model = model
    agent.config.provider = provider
    emit(agent.plugins.extensions, "error", {"message": message})


def traced(project):
    lines = (project / ".beeagent" / "trace.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_trace_keeps_the_failure_and_drops_the_transcript(project):
    agent = load(project, "trace")
    long_body = ("Error calling provider: the pool answered 502 "
                 + "padding " * 80 + "THE_MESSAGE_BODY")
    fail(agent, long_body)

    raw = (project / ".beeagent" / "trace.jsonl").read_text(encoding="utf-8")
    entry = traced(project)[-1]
    assert entry["status"] == 502 and entry["kind"] == "error"
    assert entry["model"] == "qwen3.6" and entry["provider"] == "pool"
    assert entry["at"] and len(entry["error"]) <= 200
    assert "THE_MESSAGE_BODY" not in raw, "a failure log is not a transcript"

    text = plain(run(agent, "/trace"))
    assert "qwen3.6" in text and "502" in text


def test_trace_points_at_the_pool_when_the_models_were_never_reached(project):
    agent = load(project, "trace")
    fail(agent, "the pool answered 502 —")
    fail(agent, "the pool answered 502 —", model="glm-5.3")
    text = plain(run(agent, "/trace"))
    assert "502" in text and "pool" in text and "2" in text


def test_trace_reads_a_rate_limit_as_a_budget_question(project):
    agent = load(project, "trace")
    fail(agent, "Error calling provider: 429 daily_limit_exceeded")
    assert "/seat" in plain(run(agent, "/trace"))


def test_trace_notes_a_retry_without_inventing_a_cause(project):
    agent = load(project, "trace")
    agent.config.model = "gpt-4"
    agent.config.provider = "g4f"
    emit(agent.plugins.extensions, "retry", {"attempt": 2})

    entry = traced(project)[0]
    assert entry["kind"] == "retry" and entry["status"] is None
    assert "attempt" in entry["error"]
    assert "gpt-4" in plain(run(agent, "/trace"))


def test_a_number_that_is_not_an_http_status_stays_out_of_the_status_column(project):
    agent = load(project, "trace")
    fail(agent, "the model refused 8192 tokens over a 2048 window", provider="g4f")
    assert traced(project)[0]["status"] is None


def test_trace_keeps_its_file_inside_the_cap(project):
    agent = load(project, "trace")
    for i in range(250):
        fail(agent, f"the pool answered 502 — line {i}")
    entries = traced(project)
    assert len(entries) == 200
    assert entries[-1]["error"].endswith("line 249")
    assert entries[0]["error"].endswith("line 50")


def test_trace_skips_a_line_it_cannot_read(project):
    agent = load(project, "trace")
    fail(agent, "the pool answered 502 — once")
    target = project / ".beeagent" / "trace.jsonl"
    target.write_text(target.read_text(encoding="utf-8") + '{"cut off', encoding="utf-8")
    assert "502" in plain(run(agent, "/trace"))
    assert len(module("trace").read(agent)) == 1, "a torn line is skipped, not fatal"


def test_trace_says_so_when_nothing_has_failed(project):
    agent = load(project, "trace")
    assert "trace.jsonl" in plain(run(agent, "/trace"))


def test_a_trace_file_that_cannot_be_written_does_not_stop_the_answer(project, monkeypatch):
    agent = load(project, "trace")
    monkeypatch.setattr(module("trace"), "path",
                        lambda api: Path("Z:/definitely/not/here/trace.jsonl"))
    fail(agent, "the pool answered 502 — still answering")
    assert not (project / ".beeagent" / "trace.jsonl").exists()
    assert "trace.jsonl" in plain(run(agent, "/trace"))


# --- scoreboard -----------------------------------------------------------------

class Clock:
    """The only way to measure a 23.7 s model in a test without waiting for it."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def time(self):
        return 1758000000.0

    def advance(self, seconds):
        self.now += seconds

    def strftime(self, fmt, stamp=None):
        return real_time.strftime(fmt, stamp or real_time.localtime())

    def localtime(self, stamp=None):
        return real_time.localtime(stamp)


def answer(agent, clock, model, seconds, event="done", message=""):
    agent.config.model = model
    emit(agent.plugins.extensions, "status", {})
    clock.advance(seconds)
    emit(agent.plugins.extensions, event,
         {"message": message} if event == "error" else {"text": "an answer"})


@pytest.fixture()
def timed(project, monkeypatch):
    """`status` opens a turn and `done` closes it, so the clock belongs to the test."""
    agent = load(project, "scoreboard")
    clock = Clock()
    monkeypatch.setattr(module("scoreboard"), "time", clock)
    return agent, clock


def test_board_ranks_by_what_this_machine_saw(timed):
    agent, clock = timed
    answer(agent, clock, "kimi-k2-7-code", 2.0)
    answer(agent, clock, "kimi-k2-7-code", 3.0)
    answer(agent, clock, "qwen3.6", 5.0, "error", "the pool answered 502")
    answer(agent, clock, "qwen3.6", 4.0)
    answer(agent, clock, "qwen3.6", 4.0, "error", "the pool answered 502")

    board = stored(agent, "board.json")
    assert board["kimi-k2-7-code"]["ok"] == 2 and board["kimi-k2-7-code"]["fail"] == 0
    assert board["qwen3.6"]["ok"] == 1 and board["qwen3.6"]["fail"] == 2

    text = plain(run(agent, "/board"))
    assert text.index("kimi-k2-7-code") < text.index("qwen3.6"), "what worked comes first"
    assert "502" in text, "a failure keeps its reason next to the model"


def test_median_latency_not_the_average(timed):
    agent, clock = timed
    for seconds in (1.0, 2.0, 100.0):
        answer(agent, clock, "grok-code-fast-1", seconds)
    assert "2.0s" in plain(run(agent, "/board"))
    assert stored(agent, "board.json")["grok-code-fast-1"]["lat"] == [1.0, 2.0, 100.0]


def test_a_model_the_provider_does_not_serve_is_named_and_buried(timed):
    agent, clock = timed
    emit(agent.plugins.extensions, "model_switched", {"from": "gpt-4", "to": "glm-5.3"})
    answer(agent, clock, "glm-5.3", 3.0)

    board = stored(agent, "board.json")
    assert board["gpt-4"]["refused"] is True
    assert board["glm-5.3"]["ok"] == 1, "the model that answered took the credit"

    text = plain(run(agent, "/board"))
    assert "served" in text
    assert text.index("glm-5.3") < text.index("gpt-4")


def test_a_retry_is_counted_separately_from_a_failure(timed):
    agent, clock = timed
    agent.config.model = "gpt-4"
    emit(agent.plugins.extensions, "status", {})
    emit(agent.plugins.extensions, "retry", {"attempt": 2})
    emit(agent.plugins.extensions, "done", {"text": "late, but there"})

    row = stored(agent, "board.json")["gpt-4"]
    assert row["retries"] == 1 and row["ok"] == 1 and row["fail"] == 0


def test_the_latency_window_stays_small(timed):
    agent, clock = timed
    for _ in range(30):
        answer(agent, clock, "kimi-k2-6", 1.0)
    assert len(stored(agent, "board.json")["kimi-k2-6"]["lat"]) == 12


def test_board_before_the_first_answer(project):
    agent = load(project, "scoreboard")
    assert "/board" in plain(run(agent, "/board"))


def test_a_torn_board_file_restarts_the_count_instead_of_taking_the_command_down(timed):
    agent, clock = timed
    target = Path(agent.workdir) / ".beeagent" / "board.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"gpt-4": {"ok": ', encoding="utf-8")
    answer(agent, clock, "gpt-4", 2.0)
    assert "gpt-4" in plain(run(agent, "/board"))
    assert stored(agent, "board.json")["gpt-4"]["ok"] == 1


# --- compact --------------------------------------------------------------------

def test_compact_clears_the_chrome_only_on_a_phone_like_terminal(project, monkeypatch):
    monkeypatch.setenv("COLUMNS", "58")
    agent = load(project, "compact")
    assert agent.plugins.load_errors == []
    assert skin.get("banner") == "one-line"
    assert skin.get("frame") == "none", "a border is two of the fifty-eight columns"


def test_compact_leaves_a_wide_terminal_exactly_as_it_found_it(project, monkeypatch):
    monkeypatch.setenv("COLUMNS", "140")
    load(project, "compact")
    assert skin.get("banner") == "shimmer"
    assert skin.get("frame") == "rounded"


def test_the_narrow_opening_is_one_line_and_carries_no_logo(project, monkeypatch):
    from rich.console import Console

    from beeagent.ui import components

    monkeypatch.setenv("COLUMNS", "58")
    load(project, "compact")

    original = components.console
    buffer = io.StringIO()
    components.console = Console(width=58, file=buffer, force_terminal=False)
    try:
        components.print_banner()
    finally:
        components.console = original

    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1, buffer.getvalue()
    assert "BeeCode" in lines[0] and "█" not in lines[0]
    assert len(lines[0]) <= 58, "the 76-column logo wrapped into blocks on a phone"


def test_compact_changes_layout_and_not_the_palette(project, monkeypatch):
    monkeypatch.setenv("COLUMNS", "58")
    load(project, "compact")
    assert "border_style" not in skin.value("frame")
    assert callable(skin.value("banner"))
