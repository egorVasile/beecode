"""Autosave: checkpoints that survive a killed terminal, and what a start says about it.

The audit's headline was that a conversation reached the disk only when the REPL
loop ended. Every test below is the shape of that loss: something dies, gets
refused by the disk, or the clock moves — and the file is checked, not the log.

Time is injected everywhere rather than waited for, and the two clocks are
deliberately separate: `monotonic` decides *when* to write (it cannot jump, so a
phone syncing its time mid-turn cannot freeze the checkpointing), `now` decides
what is *stale* (it can jump, so the pruning rules are written to expect that).
`datetime.now()` appears in this file only inside session ids.
"""
import json
import os
import stat
import sys
from pathlib import Path

import pytest

from beeagent.core import autosave
from beeagent.core.autosave import AutoSaver, trim_setting, trim_tool_bodies
from beeagent.core.session import Session, read_head


class Clock:
    """A clock a test can move, forwards or backwards, mid-session."""

    def __init__(self, now: float = 1_700_000_000.0, monotonic: float = 0.0):
        self.now = now
        self.monotonic = monotonic

    def time(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds
        self.monotonic += seconds

    def jump_back(self, seconds: float) -> None:
        """Only the wall clock moves: that is what a clock jump *is*."""
        self.now -= seconds


class FakeStream:
    def __getattr__(self, name):
        return lambda *a, **k: None


def session_with(turns: int, body: str = "результат") -> Session:
    """A transcript in the shape the loop actually writes: user, call, result."""
    session = Session()
    for index in range(turns):
        session.add_user_message(f"вопрос {index}")
        session.add_assistant_message(f"[tool result] asked",
                                      tool_calls=[{"tool": "read", "args": {"path": "a"}}])
        session.add_tool_result(f"[tool result] tool=read error=False\n{body}")
    return session


def saved_rows(workdir, session_id) -> dict:
    path = Path(workdir) / ".beeagent" / "sessions" / f"{session_id}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def file_of(workdir, session_id) -> Path:
    return Path(workdir) / ".beeagent" / "sessions" / f"{session_id}.json"


# --- rule 1: incremental checkpointing -----------------------------------------

def test_a_process_that_dies_mid_turn_leaves_every_earlier_turn_on_disk(tmp_path):
    """The bug in one line: a crash used to cost the whole session, not one turn."""
    clock = Clock()
    session = session_with(3)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    for _ in range(3):
        saver.checkpoint()
        clock.tick(5)

    # The fourth turn is in flight when the process is killed: nothing more is
    # checkpointed, and the exception escapes the loop exactly like a real one.
    session.add_user_message("the turn that never finished")
    with pytest.raises(ZeroDivisionError):
        1 / 0

    on_disk = Session.load(session.session_id, str(tmp_path))
    assert len(on_disk.messages) == 9, "three whole turns survived"
    assert [m.content for m in on_disk.messages][-1].startswith("[tool result] tool=read")
    assert "the turn that never finished" not in json.dumps(
        [m.content for m in on_disk.messages], ensure_ascii=False)
    # And the file does not claim the session ended.
    assert read_head(file_of(tmp_path, session.session_id))["closed"] is False


def test_a_checkpoint_lands_for_every_turn_when_turns_are_not_a_burst(tmp_path):
    """Seconds of model time between turns means a write per turn, no coalescing."""
    clock = Clock()
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=3,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    actions = []
    for _ in range(4):
        actions.append(saver.checkpoint().action)
        clock.tick(4)                      # every turn arrives outside the window
    assert actions == ["written"] * 4
    assert saver.writes == 4 and saver.coalesced == 0


def test_turns_faster_than_the_window_coalesce_into_one_write(tmp_path):
    """No more than one write per window while turns fly — flash is not that cheap."""
    clock = Clock()
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=3,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    actions = []
    for _ in range(10):
        actions.append(saver.checkpoint().action)
        clock.tick(1)                      # a turn per second, window is three
    assert actions.count("written") <= 4, f"one write per 3s window, got {actions}"
    assert saver.coalesced >= 5
    # One turn lands inside the window after the last write: the state is owed.
    session.add_user_message("eleventh")
    assert saver.checkpoint().action == "coalesced"
    assert saver.pending() is True
    assert saver.flush().saved
    assert saver.pending() is False
    assert len(Session.load(session.session_id, str(tmp_path)).messages) == len(session.messages)


def test_a_burst_never_holds_the_last_state_longer_than_one_window(tmp_path):
    """The debounce is bounded: stale pending bytes get written mid-burst, not at exit."""
    clock = Clock()
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=3,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    saver.checkpoint()
    # Turns keep arriving inside 3s of the previous *write*, but the pending state
    # itself is now older than the window.
    clock.monotonic = 4.5
    session.add_user_message("fourth")
    assert saver.checkpoint().saved, "stale pending is written even during a burst"


def test_a_wall_clock_that_jumps_backwards_does_not_stop_checkpointing(tmp_path):
    """A phone syncing its time mid-session must not buy us silent amnesia."""
    clock = Clock(monotonic=0.0)
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=3,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    written = 0
    for step in range(6):
        clock.tick(4)
        if step == 3:
            clock.jump_back(86400 * 30)      # an hour back in wall time, not in uptime
        session.add_user_message(f"turn {step}")
        written += saver.checkpoint().saved
    assert written == 6


def test_checkpoints_leave_no_temporary_files_and_never_collide(tmp_path):
    """Two windows on one session id used to share `<id>.json.tmp` and overwrite each other."""
    session = session_with(2)
    first = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0)
    second = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0)
    for _ in range(6):
        first.checkpoint()
        second.checkpoint()
    leftovers = [p.name for p in file_of(tmp_path, session.session_id).parent.iterdir()
                 if p.name != f"{session.session_id}.json"]
    assert leftovers == [], f"a writer left its scratch file behind: {leftovers}"
    assert len(Session.load(session.session_id, str(tmp_path)).messages) == 6


def test_a_clean_save_marks_the_file_finished(tmp_path):
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0)
    saver.checkpoint()
    assert read_head(file_of(tmp_path, session.session_id))["closed"] is False
    assert saver.close(session).saved
    assert read_head(file_of(tmp_path, session.session_id))["closed"] is True


def test_an_expensive_checkpoint_buys_a_longer_window(tmp_path):
    """The debounce is self-throttling: the write that costs 2 s is not repeated 4 turns later.

    The clock here is a fake that advances *during* the save, which is what a slow
    drive does to the real monotonic timer — the throttle is measured, not assumed.
    """
    clock = Clock()
    session = session_with(1)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=3,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    real_save = session.save

    def slow_save(*args, **kwargs):
        clock.monotonic += 2.0                 # this write cost two seconds
        return real_save(*args, **kwargs)

    session.save = slow_save
    assert saver.checkpoint().saved
    assert saver._window() == 6.0, "2s of writing stretches a 3s window to 2*3"
    assert saver.status()["last_write_ms"] == 2000.0

    clock.monotonic += 4.0                     # a turn 4s later: inside 6, outside 3
    assert saver.checkpoint().action == "coalesced"
    clock.monotonic += 2.5
    session.save = real_save                   # a cheap write tightens it back
    assert saver.checkpoint().saved
    assert saver._window() == 3.0, "and it does not stay stretched for nothing"


def test_the_throttle_is_capped_so_a_crash_cannot_hide_behind_it(tmp_path):
    """A 15 s window is the worst case; past that the file would stop being a net."""
    clock = Clock()
    saver = AutoSaver(workdir=str(tmp_path), session=session_with(1),
                      debounce_seconds=3, now=clock.time, monotonic=lambda: clock.monotonic)
    saver.last_write_seconds = 600.0           # a write that took ten seconds
    assert saver._window() == autosave.MAX_DEBOUNCE_SECONDS


# --- rule 2: crash recovery on start -------------------------------------------

def test_an_unclean_end_is_reported_once_and_reads_back(tmp_path):
    """Silence was the bug: the user must not find out by noticing history is short."""
    lost = session_with(4)
    saver = AutoSaver(workdir=str(tmp_path), session=lost, debounce_seconds=0)
    saver.checkpoint()                                  # ...and the process died here

    line = autosave.recovered_notice(str(tmp_path), current_id="a_new_session")
    assert "uncleanly" in line and str(len(lost.messages)) in line and lost.session_id in line
    assert "/continue" in line

    recovered = autosave.recovered_session(str(tmp_path))
    assert recovered.session_id == lost.session_id
    assert [m.content for m in recovered.messages] == [m.content for m in lost.messages]

    # Said once: the next start has nothing new to tell.
    assert autosave.recovered_notice(str(tmp_path), current_id="a_new_session") == ""


def test_a_session_the_user_walked_away_from_is_not_reported_as_a_crash(tmp_path):
    """/reset and /continue hand the saver a new session; the old one is finished."""
    abandoned = session_with(2)
    saver = AutoSaver(workdir=str(tmp_path), session=abandoned, debounce_seconds=0)
    saver.checkpoint()
    saver.adopt(session_with(1))

    assert read_head(file_of(tmp_path, abandoned.session_id))["closed"] is True
    assert autosave.recovered_notice(str(tmp_path), current_id="whatever") == ""


def test_a_session_saved_before_this_code_existed_is_not_reported(tmp_path):
    """A legacy file has no `closed` key: upgrading must not invent a crash."""
    legacy = session_with(2)
    legacy.save(str(tmp_path))
    path = file_of(tmp_path, legacy.session_id)
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["closed"], data["saved_at"], data["message_count"], data["checkpoint"]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    assert autosave.find_unclean(str(tmp_path)) == []
    assert autosave.recovered_notice(str(tmp_path)) == ""
    # It is still a session the user can open, though.
    assert legacy.session_id in Session.list_sessions(str(tmp_path))
    # And an unreadable head is never a deletion candidate either.
    assert autosave.prune_sessions(str(tmp_path), max_sessions=1, max_age_days=0) == []
    assert path.exists()


def test_the_repl_hook_checkpoints_on_the_events_that_mean_a_turn_is_over(tmp_path, monkeypatch):
    """The wiring in `ui/repl.py`, not just the object it calls."""
    from beeagent.ui import repl

    session = session_with(1)
    notices = []
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0,
                      on_message=notices.append)
    monkeypatch.setattr(repl, "_AUTOSAVER", saver, raising=False)
    monkeypatch.setattr(repl, "get_stream", lambda: FakeStream(), raising=True)
    monkeypatch.setattr(repl, "render_tool_end", lambda *a, **k: None, raising=True)
    try:
        session.add_tool_result("[tool result] tool=bash error=False\nok")
        repl.handle_callback("tool_end", {"tool": "bash", "args": {}, "output": "ok",
                                          "error": False})
        session.add_assistant_message("готово")
        repl.handle_callback("done", {"text": "готово"})
        # A token is not a turn.
        repl.handle_callback("stream_delta", {"text": "…" * 50})
    finally:
        monkeypatch.setattr(repl, "_AUTOSAVER", None, raising=False)

    saved = Session.load(session.session_id, str(tmp_path))
    assert len(saved.messages) == len(session.messages) == 5
    assert saver.writes == 2 and notices == []
    assert read_head(file_of(tmp_path, session.session_id))["closed"] is False


# --- rule 3: a failed checkpoint is a warning, never an ending ------------------

@pytest.fixture
def unwritable(tmp_path):
    """A folder the store cannot write into, on every platform.

    `.beeagent` occupied by a regular file makes the sessions path impossible to
    create, which is the same `OSError` a full disk, a deleted folder or an
    antivirus holding the file open produces — without needing root to set up, and
    without depending on `chmod` meaning anything on Windows.
    """
    (tmp_path / ".beeagent").write_text("not a folder", encoding="utf-8")
    return tmp_path


def test_a_failing_checkpoint_warns_once_with_the_reason_and_keeps_going(
        tmp_path, unwritable):
    session = session_with(1)
    notices = []
    saver = AutoSaver(workdir=str(unwritable), session=session, debounce_seconds=0,
                      on_message=notices.append)
    results = []
    for turn in range(5):
        session.add_user_message(f"turn {turn}")
        results.append(saver.checkpoint())

    assert [r.action for r in results] == ["failed"] * 5
    assert len(notices) == 1, f"the warning became a nag: {notices}"
    assert "autosave cannot write its checkpoint" in notices[0]
    assert "OSError" in notices[0] or "Error" in notices[0]
    # The conversation is the point: nothing raised, and memory still holds it all.
    assert len(session.messages) == 8
    assert saver.pending() is True
    assert saver.status()["failures"] == 5


def test_the_warning_names_the_opt_in_that_makes_the_file_smaller(unwritable):
    session = session_with(1)
    notices = []
    saver = AutoSaver(workdir=str(unwritable), session=session, debounce_seconds=0,
                      on_message=notices.append)
    saver.checkpoint()
    assert autosave.TRIM_ENV in notices[0]
    assert str(saver.tool_body_limit) in notices[0]

    # ...and stops advertising it once trimming is on.
    notices.clear()
    second = AutoSaver(workdir=str(unwritable), session=session_with(1), trim=1,
                       debounce_seconds=0, on_message=notices.append)
    second.checkpoint()
    assert len(notices) == 1 and autosave.TRIM_ENV not in notices[0]


def test_a_final_save_that_does_not_land_is_said_every_time(unwritable):
    """Rule 3's other half: a checkpoint failing must never look like a save."""
    session = session_with(2)
    notices = []
    saver = AutoSaver(workdir=str(unwritable), session=session, debounce_seconds=0,
                      on_message=notices.append)
    saver.checkpoint()
    assert not any("NOT saved" in line for line in notices)   # checkpoint: said once
    first = saver.close(session)
    assert first.action == "failed" and not first.saved
    assert [line for line in notices if "NOT saved" in line]
    before = len(notices)
    saver = AutoSaver(workdir=str(unwritable), session=session, debounce_seconds=0,
                      on_message=notices.append)
    saver.close(session)
    assert len([line for line in notices[before:] if "NOT saved" in line]) == 1


def test_a_locked_file_is_the_same_story_on_windows(tmp_path, monkeypatch):
    """Not a missing folder: a disk that refuses *this* write, right now."""
    session = session_with(1)
    notices = []
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0,
                      on_message=notices.append)
    saver.checkpoint()                        # the first one is allowed through

    real_replace = os.replace

    def locked(src, dst, *a):
        raise PermissionError(13, "The process cannot access the file")

    monkeypatch.setattr(os, "replace", locked)
    session.add_user_message("после блокировки")
    assert saver.checkpoint().action == "failed"
    monkeypatch.setattr(os, "replace", real_replace)
    assert saver.flush().saved                 # the conversation carries on, on disk
    assert len(notices) == 1                   # and the lock was mentioned once


def test_a_read_only_folder_is_survived_when_the_platform_bothers_to_enforce_it(
        tmp_path):
    if os.name == "nt":                        # chmod on Windows sets an attribute
        pytest.skip("read-only directories are not enforced on Windows")          # nothing real
    root = tmp_path / "ro"
    root.mkdir()
    (root / ".beeagent").mkdir()
    (root / ".beeagent" / "sessions").mkdir()
    os.chmod(str(root / ".beeagent" / "sessions"), stat.S_IRUSR | stat.S_IXUSR)
    try:
        saver = AutoSaver(workdir=str(root), session=session_with(1), debounce_seconds=0)
        assert saver.checkpoint().action in ("failed", "written")
    finally:
        os.chmod(str(root / ".beeagent" / "sessions"), 0o700)


# --- rule 4: the bounded pile, announced ----------------------------------------

def _write_session(workdir, index, clock, turns: int = 1) -> Session:
    """A session written `index` ticks apart from the last one, and closed.

    The bounds are the test's, not the defaults: `close()` prunes, and a helper
    that quietly swept the folder before the assertion ran would prove nothing.
    """
    session = session_with(turns)
    clock.tick(1000)
    saver = AutoSaver(workdir=str(workdir), session=session, debounce_seconds=0,
                      max_sessions=999, max_age_days=0,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    saver.checkpoint()
    saver.close(session)
    return session


def test_the_cap_prunes_oldest_first_and_names_everything_it_removed(tmp_path):
    clock = Clock()
    sessions = [_write_session(tmp_path, index, clock) for index in range(6)]
    assert len(list((tmp_path / ".beeagent" / "sessions").glob("*.json"))) == 6

    removed = autosave.prune_sessions(str(tmp_path), max_sessions=3, max_age_days=0,
                                      active_id=sessions[-1].session_id, now=clock.time)
    names = [name for name, _ in removed]
    assert names == [sessions[0].session_id, sessions[1].session_id,
                     sessions[2].session_id], "oldest go first"
    assert all(why == "past the 3-session cap" for _n, why in removed)
    for gone in names:
        assert not file_of(tmp_path, gone).exists()
    for kept in sessions[3:]:
        assert file_of(tmp_path, kept.session_id).exists()

    # And the removal is said out loud, with the file it names.
    line = autosave.pruned_line(removed[0][0], removed[0][1], str(tmp_path))
    assert removed[0][0] in line and "removed old session" in line
    assert str(tmp_path) in line

    # The saver reports through its own channel on the way out of a session.
    _write_session(tmp_path, 6, clock)
    _write_session(tmp_path, 7, clock)
    before = len(list((tmp_path / ".beeagent" / "sessions").glob("*.json")))
    notices = []
    saver = AutoSaver(workdir=str(tmp_path), session=sessions[-1], max_sessions=2,
                      max_age_days=0, on_message=notices.append,
                      now=clock.time, monotonic=lambda: clock.monotonic)
    saver.close(sessions[-1])
    after = len(list((tmp_path / ".beeagent" / "sessions").glob("*.json")))
    assert before - after == len(notices) == len(saver.removed), \
        f"one line per deleted file: {notices}"
    assert after == 2 and all("removed old session" in line for line in notices)
    # The session that was open is not among the gone, whoever its stamp says is oldest.
    assert file_of(tmp_path, sessions[-1].session_id).exists()


def test_the_file_of_the_session_open_right_now_is_never_pruned(tmp_path):
    clock = Clock()
    old = _write_session(tmp_path, 0, clock)
    for index in range(1, 5):                       # four newer sessions on top
        _write_session(tmp_path, index, clock)

    live = AutoSaver(workdir=str(tmp_path), session=old, max_sessions=1,
                     max_age_days=0, now=clock.time, monotonic=lambda: clock.monotonic)
    removed = live.prune(active_id=old.session_id)
    assert file_of(tmp_path, old.session_id).exists(), "the open transcript survived"
    assert len(removed) == 4, "a cap of one leaves room for nothing else"
    assert not any(old.session_id in line for line in removed)
    assert list((tmp_path / ".beeagent" / "sessions").glob("*.json")) == [
        file_of(tmp_path, old.session_id)]


def test_age_pruning_removes_stale_files_and_says_so(tmp_path):
    clock = Clock()
    ancient = _write_session(tmp_path, 0, clock)
    clock.tick(41 * 86400)
    recent = _write_session(tmp_path, 1, clock)

    removed = autosave.prune_sessions(str(tmp_path), max_sessions=99, max_age_days=30,
                                      now=clock.time)
    assert [name for name, _ in removed] == [ancient.session_id]
    assert "older than 30 days" in removed[0][1]
    assert not file_of(tmp_path, ancient.session_id).exists()
    assert file_of(tmp_path, recent.session_id).exists()


def test_a_clock_that_jumps_backwards_does_not_delete_the_future(tmp_path):
    """The day-rollover lesson, applied to disk: a stamp ahead of the clock is kept.

    Every session below was written at a time the clock now calls the past, but the
    clock went backwards first — so a naive `now - stamp > horizon` is true for all
    of them and would delete the transcript the user is still working in.
    """
    clock = Clock()
    sessions = [_write_session(tmp_path, index, clock) for index in range(4)]
    # Each file is stamped 1000s apart; the clock then falls back by two years.
    clock.jump_back(700 * 86400)

    removed = autosave.prune_sessions(str(tmp_path), max_sessions=99, max_age_days=30,
                                      active_id=sessions[-1].session_id, now=clock.time)
    assert removed == [], f"a backwards clock deleted files: {removed}"
    for session in sessions:
        assert file_of(tmp_path, session.session_id).exists()

    # The count cap still runs. Relative order between the files is sound — one
    # clock wrote them all — so pruning the tail of them is honest work; what the
    # jumped clock must never be allowed to do is reach the ones ahead of it.
    capped = autosave.prune_sessions(str(tmp_path), max_sessions=2, max_age_days=30,
                                     active_id=sessions[-1].session_id, now=clock.time)
    assert [name for name, _ in capped] == [sessions[0].session_id,
                                            sessions[1].session_id], "oldest first, twice"
    assert sessions[-1].session_id not in [name for name, _ in capped]
    assert file_of(tmp_path, sessions[-1].session_id).exists()
    assert file_of(tmp_path, sessions[2].session_id).exists()
    assert not file_of(tmp_path, sessions[0].session_id).exists()


def test_backups_and_foreign_files_in_the_folder_are_left_alone(tmp_path):
    """`/compact` writes `<id>.pre-compact.json` beside sessions. Not a session."""
    from beeagent.core import compact

    clock = Clock()
    session = _write_session(tmp_path, 0, clock)
    plan = compact.plan_compaction(session_with(30), keep=4)
    backup = compact.write_backup(session, plan, str(tmp_path))
    (tmp_path / ".beeagent" / "sessions" / "notes.json").write_text(
        '{"note": "mine"}', encoding="utf-8")

    removed = autosave.prune_sessions(str(tmp_path), max_sessions=1, max_age_days=0,
                                      now=lambda: clock.now + 10 ** 9)
    assert removed == []
    assert backup.exists() and read_head(backup) == {}
    # The cap counts the session it can see; a folder of one leaves nothing to do.
    assert file_of(tmp_path, session.session_id).exists()
    assert (tmp_path / ".beeagent" / "sessions" / "notes.json").exists()


# --- rule 5: the expensive bytes -------------------------------------------------

def test_a_saved_transcript_carries_every_tool_result_verbatim_by_default(tmp_path):
    body = "данные " * 5000
    session = session_with(2, body=body)
    monkey_env_off = os.environ.pop(autosave.TRIM_ENV, None)
    try:
        saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0)
        saver.checkpoint()
    finally:
        if monkey_env_off is not None:
            os.environ[autosave.TRIM_ENV] = monkey_env_off
    rows = saved_rows(tmp_path, session.session_id)["messages"]
    tool_rows = [r["content"] for r in rows if r["role"] == "tool"]
    assert tool_rows and all(body in text for text in tool_rows)
    assert saver.trim_limit == 0


def test_opt_in_trimming_drops_the_body_and_keeps_the_reference(tmp_path):
    body = "x" * 40000
    session = session_with(2, body=body)
    notices = []
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0,
                      trim=1, tool_body_limit=512, on_message=notices.append)
    saver.checkpoint()

    rows = saved_rows(tmp_path, session.session_id)["messages"]
    tool_rows = [r["content"] for r in rows if r["role"] == "tool"]
    assert tool_rows, "the transcript still has its tool results"
    for text in tool_rows:
        assert text.startswith("[tool result] tool=read error=False"), "the header stays"
        assert body not in text
        assert "bytes of this tool output were left out" in text
    # Memory keeps every byte: the model is still answering with the real output.
    assert all(body in m.content for m in session.messages if m.role == "tool")
    assert saver.trimmed_rows == len(tool_rows)
    assert saver.status()["trim_bytes"] == 512


def test_a_resumed_session_is_readable_after_trimming(tmp_path):
    session = session_with(3, body="y" * 30000)
    AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0,
              trim=1, tool_body_limit=256).close(session)
    again = Session.load(session.session_id, str(tmp_path))
    assert len(again.messages) == 9
    assert again.messages[-1].role == "tool"


def test_a_framed_tool_result_keeps_its_fence_when_trimmed():
    """A tool result is data, and trimming must not smuggle it out of the fence."""
    from beeagent.core.session import DATA_CLOSE, DATA_OPEN, frame_as_data

    framed = frame_as_data("[tool result] tool=bash error=False\n" + ("z" * 9000))
    rows = [{"role": "tool", "content": framed}]
    trimmed, count = trim_tool_bodies(rows, 256)
    assert count == 1
    text = trimmed[0]["content"]
    assert text.startswith(DATA_OPEN) and text.endswith(DATA_CLOSE)
    assert "z" * 9000 not in text


def test_user_and_assistant_text_is_never_folded_away():
    """Only what the model caused to be printed is a candidate. A refusal, a user
    paste and a long answer all travel whole."""
    long_text = "w" * 9000
    rows = [
        {"role": "user", "content": long_text},
        {"role": "assistant", "content": long_text},
        {"role": "tool", "content": f"[tool result] Unknown tool 'bash'. {long_text}"},
    ]
    trimmed, count = trim_tool_bodies(rows, 256)
    assert count == 0
    assert [row["content"] for row in trimmed] == [row["content"] for row in rows]


@pytest.mark.parametrize("value, expected", [
    ("", 0), ("0", 0), ("off", 0), ("no", 0), ("false", 0),
    ("1", autosave.TOOL_BODY_LIMIT), ("true", autosave.TOOL_BODY_LIMIT),
    ("20000", 20000), ("nonsense", autosave.TOOL_BODY_LIMIT), ("-5", 0),
])
def test_the_opt_in_switch_reads_both_as_a_switch_and_as_a_size(monkeypatch, value, expected):
    monkeypatch.delenv(autosave.TRIM_ENV, raising=False)
    monkeypatch.setenv(autosave.TRIM_ENV, value)
    assert trim_setting(None) == expected


def test_a_size_handed_in_as_a_number_is_read_as_one():
    """The programmatic form, for the caller that passes an int, not an env string."""
    assert trim_setting(-5) == 0
    assert trim_setting(0) == 0
    assert trim_setting(True) == autosave.TOOL_BODY_LIMIT
    assert trim_setting(False) == 0
    assert trim_setting(64) == 64


def test_the_environment_var_is_what_the_saver_follows_when_not_told(tmp_path, monkeypatch):
    monkeypatch.setenv(autosave.TRIM_ENV, "1")
    saver = AutoSaver(workdir=str(tmp_path), session=session_with(1), debounce_seconds=0)
    assert saver.trim_limit == autosave.TOOL_BODY_LIMIT
    assert saver.set_trim("off") == 0
    assert saver.set_trim(4096) == 4096


# --- the format itself -----------------------------------------------------------

def test_a_checkpoint_file_is_the_same_json_shape_as_a_save(tmp_path):
    """`--continue`, `/history` and the plugins read one format, not two."""
    session = session_with(2)
    saver = AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0)
    saver.checkpoint()
    data = saved_rows(tmp_path, session.session_id)
    assert set(["id", "created_at", "messages", "closed", "checkpoint", "saved_at",
                "message_count"]) <= set(data)
    assert data["checkpoint"] is True and data["closed"] is False
    assert data["message_count"] == len(data["messages"]) == 6
    keys = list(data)
    assert keys.index("messages") == len(keys) - 1, "the head stays readable without the body"


def test_reading_the_head_does_not_need_the_transcript(tmp_path):
    session = session_with(2, body="q" * 200000)
    AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0).close(session)
    head = read_head(file_of(tmp_path, session.session_id))
    assert head["closed"] is True and head["message_count"] == 6
    assert head["id"] == session.session_id


def test_the_ack_ledger_lives_outside_the_sessions_folder(tmp_path):
    session = session_with(1)
    AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0).checkpoint()
    assert autosave.recovered_notice(str(tmp_path)) != ""
    assert (tmp_path / ".beeagent" / "autosave.json").exists()
    assert not (tmp_path / ".beeagent" / "sessions" / "autosave.json").exists()
    assert Session.list_sessions(str(tmp_path)) == [session.session_id]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows holds names DOS would not")
def test_the_saved_file_survives_a_console_that_cannot_encode_it(tmp_path):
    """cp1251 consoles are real; the transcript is UTF-8 on disk either way."""
    session = session_with(1, body="привет мир «ё»")
    AutoSaver(workdir=str(tmp_path), session=session, debounce_seconds=0).close(session)
    raw = file_of(tmp_path, session.session_id).read_bytes()
    assert "привет".encode("utf-8") in raw
    assert Session.load(session.session_id, str(tmp_path)).messages[1 + 1].content.endswith("привет мир «ё»")
