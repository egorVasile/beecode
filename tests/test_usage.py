"""The usage ledger, and the one command that reads it out loud.

What these tests pin is the promise on the box: a free model still costs
something, and BeeCode has to be able to say what. The numbers are per model and
per endpoint, they survive a restart, they survive a torn file, and they stop
growing. The seat budget is asked for only when a person types `--live`, and every
request in this file is answered by the stub below — nothing here has ever
spoken to a pool, and `/stats` without the flag must not try.
"""
import json
from pathlib import Path

import httpx
import pytest
from rich.console import Console

from beeagent.core import usage


@pytest.fixture(autouse=True)
def clean_ledger(tmp_path, monkeypatch):
    """A ledger of one's own, in a folder that is not the checkout.

    `usage.PATH` is relative and read at call time, so the chdir is enough to
    re-home it; `forget()` matters because the module caches both the parsed file
    and anything it has not said yet, and a leaked cache would be one test's
    totals answering another test's question.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(usage, "PATH", Path(".beeagent") / "usage.json")
    usage.forget()
    yield usage
    usage.forget()


def render(output) -> str:
    console = Console(width=200)
    with console.capture() as captured:
        console.print(output)
    return captured.get()


# --- the ledger itself ------------------------------------------------------

def test_two_sessions_of_one_model_add_up_to_one_row(clean_ledger):
    """Per-model totals do not care who was asking.

    The same model called from two conversations is one row of three requests —
    that is the whole reason the ledger is on disk and not a counter on the agent:
    the question "what has this folder spent on gpt-4o" outlives the answer.
    """
    clean_ledger.note("answer", model="gpt-4o", provider="g4f", session="s-one",
                      messages=[{"role": "user", "content": "hello there"}],
                      answer="hi", seconds=2.0)
    clean_ledger.note("answer", model="gpt-4o", provider="g4f", session="s-one",
                      messages=[{"role": "user", "content": "and again"}],
                      answer="ho", seconds=3.0)
    clean_ledger.note("answer", model="gpt-4o", provider="g4f", session="s-two",
                      messages=[{"role": "user", "content": "third"}],
                      answer="hum", seconds=4.0)

    clean_ledger.forget()                       # as if BeeCode had been restarted
    rows = clean_ledger.snapshot()["models"]
    assert len(rows) == 1, rows
    assert rows[0]["requests"] == 3
    assert rows[0]["stream_seconds"] == pytest.approx(9.0)
    assert clean_ledger.session_row("s-one")["requests"] == 2
    assert clean_ledger.session_row("s-two")["requests"] == 1
    assert clean_ledger.session_row("s-one")["stream_seconds"] == pytest.approx(5.0)


def test_the_same_model_through_two_endpoints_is_two_rows(clean_ledger):
    """A request through g4f and one through a pool seat are not the same spending.

    They are billed by different things — someone else's quota and your daily
    allowance — so folding them into one row would report a seat as less used
    than it is.
    """
    clean_ledger.note("answer", model="kimi-k2", provider="g4f", session="s",
                      prompt="a b c", answer="x")
    clean_ledger.note("answer", model="kimi-k2", provider="pool", session="s",
                      prompt="a b c", answer="x")
    rows = {(r["model"], r["provider"]) for r in clean_ledger.snapshot()["models"]}
    assert rows == {("kimi-k2", "g4f"), ("kimi-k2", "pool")}
    assert [p["provider"] for p in clean_ledger.by_provider()] == ["g4f", "pool"]


def test_a_cache_hit_is_not_a_request(clean_ledger):
    """Economy mode answered, so nothing was asked and nothing was spent.

    Counting a hit as a request would tell a person their seat is half gone when
    they have not used it at all — the opposite of the number they came for.
    """
    clean_ledger.note("cache", model="gpt-4o", provider="g4f", session="s")
    clean_ledger.note("cache", model="gpt-4o", provider="g4f", session="s")
    clean_ledger.note("answer", model="gpt-4o", provider="g4f", session="s",
                      prompt="one two three", answer="four five")
    row = clean_ledger.snapshot()["models"][0]
    assert (row["requests"], row["cache_hits"]) == (1, 2)


def test_a_refusal_costs_a_request_and_no_tokens(clean_ledger):
    """An endpoint that said no still answered, and the seat still paid.

    The tokens are not invented: nothing arrived, so nothing is countable, and a
    ledger that guessed the size of a refusal is a ledger nobody believes.
    """
    clean_ledger.note("error", model="glm-5.2", provider="pool", session="s",
                      error="429 today's budget is spent", seconds=1.5)
    row = clean_ledger.snapshot()["models"][0]
    assert (row["requests"], row["errors"], row["completion_tokens"]) == (1, 1, 0)
    assert "429" in clean_ledger.snapshot()["refusal"]


def test_reported_tokens_are_marked_and_counted_ones_are_not(clean_ledger):
    """`✔` for the endpoint's own number, `~` for ours — the `/models` convention.

    One ruler, and a reader who can tell which side of it each row is on.
    """
    clean_ledger.note("answer", model="reported", provider="pool", session="s",
                      usage={"prompt_tokens": 900, "completion_tokens": 40})
    clean_ledger.note("answer", model="counted", provider="pool", session="s",
                      messages=[{"role": "user", "content": "привет мир"}],
                      answer="да")
    rows = {r["model"]: r for r in clean_ledger.snapshot()["models"]}
    assert clean_ledger.mark_for(rows["reported"]) == "✔"
    assert clean_ledger.mark_for(rows["counted"]) == "~"
    assert rows["reported"]["prompt_tokens"] == 900
    assert rows["counted"]["prompt_tokens"] > 0


def test_an_unreadable_ledger_starts_clean_and_says_so_once(clean_ledger, tmp_path):
    """A half-written file is a fresh start with a confession, not a silent zero.

    Three things are checked together: the numbers restart, the damaged file
    survives beside the new one so the totals in it are still readable by a
    person, and the apology is given once — `/stats` twice must not say it twice.
    """
    path = tmp_path / ".beeagent" / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    torn = ('{\n  "rows": {\n    "gpt-4o@g4f": {\n      "requests": 4121,\n'
            '      "prompt_tokens": 90000\n')     # cut off mid-record, by a crash
    path.write_text(torn, encoding="utf-8")

    snap = clean_ledger.snapshot()
    assert snap["all"]["requests"] == 0, "the count restarted, as it must"
    notice = clean_ledger.take_notice()
    assert "unreadable" in notice and "usage.json.broken" in notice, notice
    assert clean_ledger.take_notice() == "", "the same confession twice is noise"

    rescue = tmp_path / ".beeagent" / "usage.json.broken"
    assert "4121" in rescue.read_text(encoding="utf-8"), "the old totals are gone"

    clean_ledger.note("answer", model="gpt-4o", provider="g4f", session="s",
                      prompt="hello", answer="hi")
    assert clean_ledger.snapshot()["all"]["requests"] == 1
    assert "4121" in rescue.read_text(encoding="utf-8"), "the new write ate the old file"


def test_a_file_that_is_not_an_object_is_treated_as_damage(clean_ledger, tmp_path):
    """`[]`, `"many"`, a stray log line — none of them is a ledger to add to."""
    path = tmp_path / ".beeagent" / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert clean_ledger.snapshot()["all"]["requests"] == 0
    assert "unreadable" in clean_ledger.take_notice()


def test_a_number_written_as_text_does_not_break_the_next_turn(clean_ledger, tmp_path):
    """A hand-edited `"requests": "many"` is dropped, not inherited.

    It parses as JSON and is not a dict of numbers, and the `+=` that reads it
    lives inside a turn the user is waiting on.
    """
    path = tmp_path / ".beeagent" / "usage.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"rows": {"m@g4f": {"requests": "many",
                                                   "prompt_tokens": 12.7}}}),
                    encoding="utf-8")
    assert clean_ledger.note("answer", model="m", provider="g4f", session="s",
                             prompt="a", answer="b") is True
    row = clean_ledger.snapshot()["models"][0]
    assert row["requests"] == 1 and row["prompt_tokens"] == 13


def test_the_cap_drops_the_oldest_and_announces_the_count(clean_ledger):
    """History that cannot stop growing is a file that rewrites itself forever.

    The row that fell off is the one nobody has asked in the longest, the one
    this turn is writing cannot be it, and the count is said out loud rather than
    discovered a year later when an old model's numbers are simply not there.
    """
    for i in range(usage.MAX_ROWS + 1):
        assert clean_ledger.note("answer", model=f"model-{i}", provider="g4f",
                                 session="one-long-conversation", prompt="x", answer="y")
    snap = clean_ledger.snapshot()
    assert len(snap["models"]) == usage.MAX_ROWS
    names = {r["model"] for r in snap["models"]}
    assert "model-0" not in names, "the oldest row is the one that goes"
    assert f"model-{usage.MAX_ROWS}" in names
    assert snap["trimmed"] == 1
    assert "1 row was dropped" in clean_ledger.take_notice()


def test_the_session_cap_protects_the_conversation_in_progress(clean_ledger):
    """/reset makes a new session; the last conversation's numbers stay, older ones go."""
    for i in range(usage.MAX_SESSIONS + 5):
        clean_ledger.note("answer", model="m", provider="g4f", session=f"s-{i}",
                          prompt="x", answer="y")
    snap = clean_ledger.snapshot()
    assert snap["conversations"] == usage.MAX_SESSIONS
    assert clean_ledger.session_row(f"s-{usage.MAX_SESSIONS + 4}") is not None
    assert clean_ledger.session_row("s-0") is None


def test_a_ledger_that_cannot_be_written_costs_the_turn_nothing(clean_ledger, tmp_path):
    """A `.beeagent` that is a file, not a folder: `note()` says False and stops.

    The user paid for that answer with their patience; the tally is not allowed to
    take it back, and not allowed to pretend the numbers are on disk either.
    """
    (tmp_path / ".beeagent").write_text("not a folder", encoding="utf-8")
    assert clean_ledger.note("answer", model="m", provider="g4f", session="s",
                             prompt="a", answer="b") is False
    assert "could not be saved" in clean_ledger.take_notice()
    # The session's own numbers are still held in memory and still add up.
    assert clean_ledger.snapshot()["all"]["requests"] == 1


def test_an_unknown_kind_is_refused_without_writing_a_row(clean_ledger):
    """A caller's typo is not a model that was asked something."""
    assert clean_ledger.note("probably-a-request", model="m", provider="g4f") is False
    assert clean_ledger.snapshot()["models"] == []
    assert clean_ledger.take_notice() == ""


def test_reading_the_ledger_creates_nothing(clean_ledger, tmp_path):
    """`/stats` in a fresh checkout must leave the checkout as it found it.

    Writing on a read is how a status command ends up in `git status`.
    """
    assert clean_ledger.snapshot()["all"]["requests"] == 0
    assert not (tmp_path / ".beeagent").exists(), "a read made a folder"
    assert not list(tmp_path.rglob("usage.json*")), "a read made a file"


def test_every_write_lands_as_one_whole_file(clean_ledger, tmp_path):
    """Atomic: a swap through a name only this write owns, or nothing happened.

    Checked from the other side — after every write the file parses, and no side
    of the write is left lying around in the folder.
    """
    clean_ledger.note("answer", model="m", provider="g4f", session="s",
                      prompt="a b", answer="c")
    left = sorted(p.name for p in (tmp_path / ".beeagent").iterdir())
    assert left == ["usage.json"], left
    assert json.loads((tmp_path / ".beeagent" / "usage.json").read_text(encoding="utf-8"))


# --- /stats -----------------------------------------------------------------

class Answer:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body if body is not None else {}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


class Pool:
    """`httpx.Client` replaced wholesale: the seat is answered, never dialled.

    `respond(url)` returns an `Answer` or the exception the connection raised.
    Every call is logged, so a test can also prove the seat token was actually
    sent — the bug this stub exists to keep out — and that a plain `/stats` made
    no call at all.
    """

    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    def install(self, monkeypatch):
        from beeagent.providers import pool as pool_mod

        monkeypatch.setattr(pool_mod.httpx, "Client", lambda *a, **kw: self)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        out = self.respond(url)
        if isinstance(out, BaseException):
            raise out
        return out

    def seen(self, needle):
        return [c for c in self.calls if needle in c["url"]]


def seat_ok(url):
    if url.endswith("/healthz"):
        return Answer(200, {"ok": True})
    return Answer(200, {"requests": 47, "requests_limit": 200, "tokens": 512000,
                        "tokens_limit": 2000000, "resets_in_seconds": 7425,
                        "approved": True})


@pytest.fixture
def pool_ctx(tmp_path, monkeypatch):
    """A BeeCode answering from a pool seat, in a folder of its own."""
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(usage, "PATH", Path(".beeagent") / "usage.json")
    usage.forget()
    config = BeeConfig(provider="pool", pool_url="http://pool.invalid",
                       pool_token="seat-token-abc", model="kimi-k2-6")
    ctx = ReplContext(agent=Agent(config=config), config=config, session=Session())
    usage.note("answer", model="kimi-k2-6", provider="pool",
               session=ctx.session.session_id, messages=[{"role": "user",
                                                          "content": "hi"}],
               answer="yo", seconds=3.0)
    yield ctx
    usage.forget()


def test_stats_prints_the_models_it_knows(pool_ctx):
    """/stats that cannot name the models it counted is a status line, not a ledger."""
    from beeagent.ui.commands import dispatch

    text = render(dispatch(pool_ctx, "/stats").output)
    assert "kimi-k2-6" in text
    assert "pool" in text
    assert "This conversation" in text
    assert "3.0s" in text, "seconds spent waiting are the point of the row"
    assert "not asked" in text, "`--live` was not typed, and the table says so"
    assert usage.session_row(pool_ctx.session.session_id)["requests"] == 1


def test_stats_never_crashes_on_a_ledger_nothing_was_written_to(tmp_path, monkeypatch):
    """A first run: no folder, no file, no numbers, one honest sentence."""
    from beeagent.config.schema import BeeConfig
    from beeagent.core.agent import Agent
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext, dispatch

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(usage, "PATH", Path(".beeagent") / "usage.json")
    usage.forget()
    config = BeeConfig()
    ctx = ReplContext(agent=Agent(config=config), config=config, session=Session())
    text = render(dispatch(ctx, "/stats").output)
    assert "ledger is empty" in text
    assert "model" in text.lower()
    assert not (tmp_path / ".beeagent" / "usage.json").exists()


def test_live_stats_show_the_seat_budget_and_send_the_token(pool_ctx, monkeypatch):
    """The number the free-model pitch actually owes: how much of the seat is left.

    Also the regression the stub is here for: `/v1/seat` must be asked *with* the
    seat token, and with a short timeout rather than the ninety seconds enrolment
    allows itself.
    """
    from beeagent.ui.commands import dispatch

    wire = Pool(seat_ok).install(monkeypatch)
    text = render(dispatch(pool_ctx, "/stats --live").output)

    seat = wire.seen("/v1/seat")
    assert seat, wire.calls
    assert seat[0]["headers"]["Authorization"] == "Bearer seat-token-abc"
    assert seat[0]["timeout"] <= 15, "a seat ask runs on the prompt's own thread"
    assert "47 / 200" in text
    assert "153 requests" in text, "what is left, not only what is gone"
    assert "2h 03m" in text, "the reset the pool reported"


def test_live_stats_show_the_seat_even_when_the_pool_names_no_reset(pool_ctx, monkeypatch):
    """A pool that answers without a reset line is still asked, and still quoted."""
    from beeagent.ui.commands import dispatch

    def bare(url):
        return Answer(200, {"ok": True}) if url.endswith("/healthz") \
            else Answer(200, {"requests": 3, "requests_limit": 10})

    Pool(bare).install(monkeypatch)
    text = render(dispatch(pool_ctx, "/stats --live").output)
    assert "3 / 10" in text
    assert "resets in" not in text, "no reset was reported, none was invented"


def test_live_stats_keep_the_local_numbers_when_the_pool_is_asleep(pool_ctx, monkeypatch):
    """A refused or unreachable pool costs one line and nothing else.

    The box sleeps after fifteen idle minutes; the ledger is on the disk in front
    of it, and the two must not be confused for each other.
    """
    from beeagent.ui.commands import dispatch

    Pool(lambda url: httpx.ConnectError("connection refused")).install(monkeypatch)
    text = render(dispatch(pool_ctx, "/stats --live").output)
    assert "did not answer" in text
    assert "kimi-k2-6" in text, "the local totals stayed"
    assert usage.session_row(pool_ctx.session.session_id)["requests"] == 1
    assert "47 / 200" not in text


def test_live_stats_say_what_the_pool_refused(pool_ctx, monkeypatch):
    """'unreachable' and 'this seat is not known' are different things to do about it."""
    from beeagent.ui.commands import dispatch

    def refused(url):
        return Answer(200, {"ok": True}) if url.endswith("/healthz") \
            else Answer(403, {"error": "this seat is waiting for approval"})

    Pool(refused).install(monkeypatch)
    text = render(dispatch(pool_ctx, "/stats --live").output)
    assert "waiting for approval" in text
    assert "kimi-k2-6" in text


def test_stats_without_the_flag_never_reaches_for_the_pool(pool_ctx, monkeypatch):
    """No network at boot, and none on an ordinary `/stats` either.

    The stub raises if it is used, so this passes only while the flag is the only
    door to the wire.
    """
    from beeagent.ui.commands import dispatch

    def tripwire(url):
        raise AssertionError(f"/stats asked the pool without being told to: {url}")

    wire = Pool(tripwire).install(monkeypatch)
    render(dispatch(pool_ctx, "/stats").output)
    render(dispatch(pool_ctx, "/stats --all").output)
    assert wire.calls == []


def test_reading_the_stats_command_leaves_the_ledger_alone(pool_ctx, tmp_path):
    """`/stats` reports what a turn cost; it is not itself a turn."""
    from beeagent.ui.commands import dispatch

    before = (tmp_path / ".beeagent" / "usage.json").read_bytes()
    dispatch(pool_ctx, "/stats")
    assert (tmp_path / ".beeagent" / "usage.json").read_bytes() == before


def test_the_notice_is_said_on_the_next_stats_and_not_after_that(pool_ctx, tmp_path):
    """A ledger that lost its file says so once, in the place a person will read it."""
    from beeagent.ui.commands import dispatch

    (tmp_path / ".beeagent" / "usage.json").write_text("{oops", encoding="utf-8")
    first = render(dispatch(pool_ctx, "/stats").output)
    second = render(dispatch(pool_ctx, "/stats").output)
    assert "unreadable" in first
    assert "unreadable" not in second


def test_stats_survives_an_agent_that_cannot_answer(tmp_path, monkeypatch):
    """No agent, no crash: the command that reports on the agent needs one to exist."""
    from beeagent.config.schema import BeeConfig
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext, dispatch

    monkeypatch.chdir(tmp_path)
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    assert "No agent" in render(dispatch(ctx, "/stats").output)
