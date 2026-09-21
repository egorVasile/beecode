"""Session identity and durability: ids, atomic saves, tolerant listing."""
import json

from beeagent.core.session import Session


def test_two_sessions_started_in_the_same_second_do_not_collide(tmp_path):
    """Second-resolution ids made the second save overwrite the first."""
    first = Session()
    second = Session()
    first.add_user_message("первый")
    second.add_user_message("второй")
    first.save(str(tmp_path))
    second.save(str(tmp_path))

    assert first.session_id != second.session_id
    ids = Session.list_sessions(str(tmp_path))
    assert sorted(ids) == sorted([first.session_id, second.session_id])
    assert Session.load(first.session_id, str(tmp_path)).messages[0].content == "первый"


def test_a_torn_session_file_is_skipped_not_offered(tmp_path):
    good = Session()
    good.add_user_message("целая")
    good.save(str(tmp_path))
    broken = tmp_path / ".beeagent" / "sessions" / "20200101_000000.json"
    broken.write_text('{"id": "20200101_000000", "messages": [{"role": "user", ', encoding="utf-8")

    assert Session.list_sessions(str(tmp_path)) == [good.session_id]
    assert Session.load(good.session_id, str(tmp_path)).messages[0].content == "целая"


def test_loading_a_broken_session_says_which_one(tmp_path):
    path = tmp_path / ".beeagent" / "sessions"
    path.mkdir(parents=True)
    (path / "20200101_000000.json").write_text("{oops", encoding="utf-8")
    try:
        Session.load("20200101_000000", str(tmp_path))
        raise AssertionError("a corrupt session must not load silently")
    except ValueError as e:
        assert "20200101_000000" in str(e)


def test_save_leaves_no_temporary_file_behind(tmp_path):
    session = Session()
    session.add_user_message("данные")
    session.save(str(tmp_path))
    leftovers = [p.name for p in (tmp_path / ".beeagent" / "sessions").glob("*.tmp")]
    assert leftovers == []
    saved = json.loads((tmp_path / ".beeagent" / "sessions" / f"{session.session_id}.json")
                       .read_text(encoding="utf-8"))
    assert saved["messages"][0]["content"] == "данные"
