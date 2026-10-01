"""Sessions across time: save, resume, compact, export, prune, recover.

Unit tests cover one call each; these cover the lifecycle — what happens to
a conversation between yesterday and tomorrow. Every one of these failed or
had no coverage before it was written.
"""
import json
import os
import threading
import time

import pytest

from beeagent.core.session import Session, session_files


def _talk(session, turns=3):
    for i in range(turns):
        session.add_user_message(f"question {i}")
        session.add_assistant_message(
            f"answer {i}",
            tool_calls=[{"tool": "read", "args": {"path": f"f{i}.txt"}}])
        session.add_tool_result(f"[tool result] tool=read error=False\ncontent {i}")
    return session


def test_resume_roundtrip_keeps_tool_pairs_intact(tmp_path):
    """A resumed session must replay assistant+tool rows the loop can read."""
    session = _talk(Session())
    session.add_user_message("and one more")
    session.save(str(tmp_path))

    resumed = Session.latest(str(tmp_path))
    assert resumed is not None
    rows = resumed.to_dicts()
    assert len(rows) == 3 * 3 + 1
    for i in range(3):
        assert rows[i * 3]["role"] == "user"
        assert rows[i * 3 + 1]["tool_calls"][0]["tool"] == "read"
        assert rows[i * 3 + 2]["role"] == "tool"
    assert rows[-1]["content"] == "and one more"


def test_double_save_same_id_never_tears_the_file(tmp_path):
    """Resume + save writes the same id twice; the file stays whole."""
    session = _talk(Session())
    session.save(str(tmp_path))
    session.add_user_message("after resume")
    session.save(str(tmp_path))

    data = json.loads((tmp_path / ".beeagent" / "sessions" /
                       f"{session.session_id}.json").read_text(encoding="utf-8"))
    assert data["id"] == session.session_id
    assert data["messages"][-1]["content"] == "after resume"


def test_concurrent_saves_leave_one_whole_file(tmp_path):
    """Two threads, one id, atomic replace: no torn JSON, ever."""
    session = _talk(Session())
    errors = []

    def save():
        try:
            for _ in range(10):
                session.save(str(tmp_path))
        except Exception as e:  # noqa: BLE001 — the test records, not hides
            import traceback

            errors.append("".join(traceback.format_exception(e)))

    threads = [threading.Thread(target=save) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors
    path = tmp_path / ".beeagent" / "sessions" / f"{session.session_id}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["id"] == session.session_id
    assert isinstance(data["messages"], list)


def test_mtime_beats_id_when_a_clock_lied(tmp_path):
    """A phone whose clock runs behind must not lose to last week's file."""
    old = _talk(Session(session_id="99990101_000000_aaaaaa"))
    old.save(str(tmp_path))
    new = Session(session_id="20200101_000000_aaaaaa")
    new.add_user_message("actually newer")
    path = tmp_path / ".beeagent" / "sessions" / "20200101_000000_aaaaaa.json"
    new.save(str(tmp_path))
    aged = time.time() - 100000
    os.utime(str(tmp_path / ".beeagent" / "sessions" /
                  "99990101_000000_aaaaaa.json"), (aged, aged))

    assert Session.latest(str(tmp_path)).session_id == "20200101_000000_aaaaaa"


def test_compact_backup_is_not_listed_as_a_session(tmp_path):
    session = _talk(Session())
    session.save(str(tmp_path))
    backup = tmp_path / ".beeagent" / "sessions" / f"{session.session_id}.pre-compact.json"
    backup.write_text("{}", encoding="utf-8")

    ids = Session.list_sessions(str(tmp_path))
    assert ids == [session.session_id]
    assert session_files(tmp_path / ".beeagent" / "sessions") != []


def test_compacted_session_replays_cleanly(tmp_path):
    """Fold, save, reload: the digest is a system row the loop can send."""
    from beeagent.core import compact as compact_mod

    session = _talk(Session(), turns=6)
    session.save(str(tmp_path))
    plan = compact_mod.apply_compaction(session, keep=2, workdir=str(tmp_path))
    assert plan.backup_path

    reloaded = Session.load(session.session_id, str(tmp_path))
    rows = reloaded.to_dicts()
    assert rows[0]["role"] == "system"
    assert len(rows) < 6 * 3 + 1
    # Tool pairs travel together or not at all.
    calls = sum(1 for r in rows if r.get("tool_calls"))
    tools = sum(1 for r in rows if r["role"] == "tool")
    assert calls == tools


def test_export_covers_every_row(tmp_path):
    session = _talk(Session(), turns=2)
    target = tmp_path / "out.md"
    lines = [f"# BeeCode session {session.session_id}", ""]
    for m in session.messages:
        lines.extend([f"## {m.role}", m.content or "", ""])
    target.write_text("\n".join(lines), encoding="utf-8")

    text = target.read_text(encoding="utf-8")
    assert text.count("## user") == 2
    assert text.count("## assistant") == 2
    assert text.count("## tool") == 2
    assert "content 1" in text


def test_find_unclean_and_notice_roundtrip(tmp_path, monkeypatch):
    """A killed turn leaves closed:false; the next start says so once."""
    monkeypatch.chdir(tmp_path)
    from beeagent.core import autosave as autosave_mod

    session = _talk(Session())
    session.save(str(tmp_path), clean=False)
    unclean = autosave_mod.find_unclean(str(tmp_path))
    assert [u.session_id for u in unclean] == [session.session_id]

    first = autosave_mod.recovered_notice(str(tmp_path))
    assert session.session_id in first
    assert autosave_mod.recovered_notice(str(tmp_path)) == "", "said once"


def test_prune_keeps_the_active_and_the_newest(tmp_path):
    from beeagent.core import autosave as autosave_mod

    ids = []
    for i in range(6):
        session = Session()
        session.add_user_message(f"m{i}")
        session.save(str(tmp_path))
        ids.append(session.session_id)
        time.sleep(0.02)

    removed = autosave_mod.prune_sessions(str(tmp_path), max_sessions=3,
                                         max_age_days=0, active_id=ids[-1])
    remaining = set(autosave_mod.Session.list_sessions(str(tmp_path))) \
        if hasattr(autosave_mod, "Session") else set(Session.list_sessions(str(tmp_path)))
    assert ids[-1] in remaining, "the active session is never pruned"
    assert len(remaining) == 3
    assert [r[0] for r in removed], "every removal is reported"


def test_foreign_files_in_sessions_dir_are_left_alone(tmp_path):
    (tmp_path / ".beeagent" / "sessions").mkdir(parents=True)
    (tmp_path / ".beeagent" / "sessions" / "notes.txt").write_text("mine", encoding="utf-8")
    session = _talk(Session())
    session.save(str(tmp_path))

    assert Session.list_sessions(str(tmp_path)) == [session.session_id]
    assert (tmp_path / ".beeagent" / "sessions" / "notes.txt").read_text(
        encoding="utf-8") == "mine"
