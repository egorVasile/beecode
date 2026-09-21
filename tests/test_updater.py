"""The update notice must be correct, rare, and never in the way."""
import asyncio
import json
import time

import pytest

from beeagent.core import updater


@pytest.fixture(autouse=True)
def no_leftover_thread(monkeypatch):
    monkeypatch.setattr(updater, "_thread", None)
    yield
    updater._thread = None


def test_versions_compare_as_numbers_not_strings():
    assert updater.is_newer("0.10.0", "0.9.9")
    assert not updater.is_newer("0.9.9", "0.10.0")
    assert not updater.is_newer("0.2.0", "0.2.0")
    assert updater.is_newer("v1.0.1", "1.0.0")


def test_a_dead_network_offers_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda **kw: "")
    body = updater.check(workdir=tmp_path, force=True)
    assert body.get("error") == "network"
    assert updater.available(tmp_path, current="0.1.0") == ""


def test_the_check_is_rare_once_the_answer_is_known(tmp_path, monkeypatch):
    calls = []

    def fake():
        calls.append(1)
        return "9.9.9"

    monkeypatch.setattr(updater, "fetch_latest", fake)
    updater.check(workdir=tmp_path)
    updater.check(workdir=tmp_path)
    assert len(calls) == 1, "an hour of restarts must not ask GitHub each time"
    updater.check(workdir=tmp_path, force=True)
    assert len(calls) == 2


def test_a_refused_release_is_not_asked_about_twice(tmp_path):
    updater.write_cache(tmp_path, checked_at=time.time(), latest="9.9.9")
    assert updater.available(tmp_path, current="0.1.0") == "9.9.9"
    updater.write_cache(tmp_path, declined="9.9.9")
    assert updater.available(tmp_path, current="0.1.0") == ""
    # The next release still gets to ask.
    updater.write_cache(tmp_path, latest="9.10.0")
    assert updater.available(tmp_path, current="0.1.0") == "9.10.0"


def test_starting_the_check_does_not_wait_for_it(tmp_path, monkeypatch):
    def slow():
        time.sleep(0.4)
        return "9.9.9"

    monkeypatch.setattr(updater, "fetch_latest", slow)
    monkeypatch.setattr(updater, "CACHE_PATH", tmp_path / "update.json")
    began = time.monotonic()
    updater.start(tmp_path)
    assert time.monotonic() - began < 0.1, "the first prompt must not wait for GitHub"
    assert updater.wait(0) is False, "still travelling"
    assert updater.wait(2) is True


def _command(agent, line):
    from beeagent.core.session import Session
    from beeagent.ui.commands import ReplContext, dispatch

    return dispatch(ReplContext(agent=agent, config=agent.config, session=Session()), line)


class _Agent:
    def __init__(self, workdir):
        from beeagent.config.schema import BeeConfig

        self.workdir = str(workdir)
        self.config = BeeConfig()


def test_update_command_offers_the_install_when_a_version_is_newer(tmp_path, monkeypatch):
    from beeagent import __version__

    monkeypatch.setattr(updater, "fetch_latest", lambda **kw: "99.0.0")
    result = _command(_Agent(tmp_path), "/update")
    assert result.action == "update"
    assert "99.0.0" in result.output.plain


def test_update_command_says_so_when_there_is_nothing_newer(tmp_path, monkeypatch):
    from beeagent import __version__

    monkeypatch.setattr(updater, "fetch_latest", lambda **kw: __version__)
    result = _command(_Agent(tmp_path), "/update")
    assert result.action is None
    assert __version__ in result.output.plain


def test_update_command_does_not_invent_an_update_when_the_server_is_silent(
        tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda **kw: "")
    result = _command(_Agent(tmp_path), "/update")
    assert result.action is None
    assert result.output is None or "did not answer" in str(result.output).lower() \
        or "не ответил" in str(result.output)


def test_the_offer_waits_for_the_thread_instead_of_hanging(tmp_path, monkeypatch):
    from beeagent.ui import repl

    asked = []

    async def never(*args, **kwargs):
        asked.append(1)
        return True

    monkeypatch.setattr(updater, "wait", lambda timeout=1.0: False)
    monkeypatch.setattr(repl, "yes_no_dialog", never)
    assert asyncio.run(repl._offer_update(_Agent(tmp_path))) is False
    assert asked == [], "no dialog while the answer is still travelling"


def test_the_offer_asks_when_a_newer_version_is_known(tmp_path, monkeypatch):
    from beeagent.ui import repl

    buttons = {}

    class FakeDialog:
        def __init__(self, **kwargs):
            buttons.update(kwargs)

        async def run_async(self):
            return False

    monkeypatch.setattr(updater, "wait", lambda timeout=1.0: True)
    monkeypatch.setattr(updater, "fetch_latest", lambda **kw: "99.0.0")
    updater.check(workdir=tmp_path, force=True)
    monkeypatch.setattr(repl, "yes_no_dialog", lambda **kwargs: FakeDialog(**kwargs))

    assert asyncio.run(repl._offer_update(_Agent(tmp_path))) is True
    # The title is painted letter by letter, so it has to be read back as one.
    assert "99.0.0" in "".join(part for _, part in buttons["title"])
    assert buttons["yes_text"] and buttons["no_text"]
    # "Later" is remembered, so the same release does not ask again.
    assert json.loads((tmp_path / ".beeagent" / "update.json").read_text())["declined"] == "99.0.0"
