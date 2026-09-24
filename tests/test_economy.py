import json
import os
import shutil
import warnings
from pathlib import Path

import pytest

from beeagent.core.parser import CommandParser
from beeagent.core.economy import EconomyManager
from beeagent.utils import cache as cache_module
from beeagent.utils.cache import ResponseCache


def test_cache_store_and_retrieve(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("hello", "gpt-4", "world")
    assert cache.get("hello", "gpt-4") == "world"


def test_cache_miss(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    assert cache.get("nonexistent", "gpt-4") is None


def test_cache_different_models(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("prompt", "gpt-4", "response4")
    cache.store("prompt", "gpt-3.5", "response35")
    assert cache.get("prompt", "gpt-4") == "response4"
    assert cache.get("prompt", "gpt-3.5") == "response35"


def test_cache_expires(tmp_path):
    """A reply about a file is not true once that file changes."""
    cache = ResponseCache(cache_dir=str(tmp_path), ttl_seconds=1)
    cache.store("prompt", "gpt-4", "answer")
    assert cache.get("prompt", "gpt-4") == "answer"
    entries = list(tmp_path.glob("*.json"))
    data = json.loads(entries[0].read_text(encoding="utf-8"))
    entries[0].write_text(json.dumps({**data, "saved_at": 0}), encoding="utf-8")
    assert cache.get("prompt", "gpt-4") is None
    assert not entries[0].exists()                 # stale entries are dropped


def test_cache_keeps_the_prompt_off_disk(tmp_path):
    """Prompts quote file contents and tool output; the key is a hash."""
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("secret path C:/users/ivan/.env", "gpt-4", "cached answer")
    body = "\n".join(f.read_text(encoding="utf-8") for f in tmp_path.glob("*.json"))
    assert "secret" not in body and "ivan" not in body
    assert "cached answer" in body


def test_cache_corrupt_entry_is_discarded(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("p", "m", "x")
    path = next(tmp_path.glob("*.json"))
    path.write_text("{ not json", encoding="utf-8")
    assert cache.get("p", "m") is None


def test_economy_manager_normal_caches_nothing():
    manager = EconomyManager(mode="normal")
    assert manager.should_cache() is False
    assert manager.cache is None


def test_economy_manager_economy(tmp_path):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    assert manager.should_cache() is True
    manager.store_cache("p", "gpt-4", "answer")
    assert manager.check_cache("p", "gpt-4") == "answer"


def test_cache_disabled_by_config(tmp_path):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_enabled=False)
    assert manager.should_cache() is False


def test_switching_mode_opens_and_closes_the_cache(tmp_path):
    manager = EconomyManager(mode="normal", cache_dir=str(tmp_path / "c"))
    assert manager.cache is None
    manager.set_mode("economy")
    assert manager.cache is not None
    manager.set_mode("normal")
    assert manager.cache is None


def test_economy_stats(tmp_path):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", "answer")
    stats = manager.get_stats()
    assert stats["mode"] == "economy"
    assert stats["requests"] == 0
    assert stats["cache"] == "on"
    assert stats["cached_answers"] == 1


# --- the fake clock ---------------------------------------------------------

class Clock:
    """Time as the one place `cache.py` reads it, turned by the test.

    Nothing sleeps here: entries age because the test says so.
    """

    def __init__(self, now: float = 1_700_000_000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds

    def stamp(self, path):
        """mtime is what the size sweep orders by — real clocks wrote it before."""
        os.utime(path, (self.now, self.now))


@pytest.fixture
def clock(monkeypatch):
    fake = Clock()
    monkeypatch.setattr(cache_module.time, "time", fake)
    return fake


def entry(manager, prompt, model="gpt-4"):
    return Path(manager.cache.cache_dir) / f"{manager.cache._key(prompt, model)}.json"


class SpyCache:
    """The handful of `ResponseCache` members the manager touches, with a memory
    of every sweep it was asked to run."""

    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.entries = {}
        self.pruned_with = []
        self.ttl_seconds = 0

    def get(self, prompt, model):
        return self.entries.get(prompt)

    def store(self, prompt, model, response):
        self.entries[prompt] = response

    def count(self):
        return len(self.entries)

    def prune(self, keep):
        self.pruned_with.append(keep)
        return 0


def _manager_over(cache, **settings):
    """A manager wired to a stand-in cache, so the write/read policy is tested
    without betting on how `ResponseCache` happens to prune today."""
    manager = EconomyManager(mode="normal", cache_dir=str(cache.cache_dir), **settings)
    manager.cache = cache
    return manager, cache


def test_a_newer_answer_is_still_served(tmp_path, clock):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_ttl_minutes=30)
    manager.store_cache("p", "gpt-4", "answer")
    clock.advance(29 * 60)
    assert manager.check_cache("p", "gpt-4") == "answer"


def test_an_entry_older_than_the_ttl_is_a_miss(tmp_path, clock):
    """`cache_ttl_minutes` is a promise about the files behind the answer."""
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_ttl_minutes=30)
    manager.store_cache("p", "gpt-4", "answer")
    assert manager.check_cache("p", "gpt-4") == "answer"

    clock.advance(31 * 60)
    assert manager.check_cache("p", "gpt-4") is None


def test_the_ttl_of_the_config_wins_over_a_cache_built_elsewhere(tmp_path, clock):
    """/mode economy hands over a ResponseCache built without the config's ttl.

    It gets the configured one anyway, so switching mode from the keyboard
    cannot quietly stretch a five-minute promise to the thirty-minute default.
    """
    manager = EconomyManager(mode="normal", cache_dir=str(tmp_path / "c"),
                             cache_ttl_minutes=5)
    manager.cache = ResponseCache(str(tmp_path / "c"))
    assert manager.cache.ttl_seconds == 300

    manager.store_cache("p", "gpt-4", "answer")
    clock.advance(6 * 60)
    assert manager.check_cache("p", "gpt-4") is None


# --- the cap ----------------------------------------------------------------

def test_writing_past_the_cap_drops_the_oldest_and_keeps_the_newest(tmp_path, clock):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_ttl_minutes=30, cache_max_entries=3)
    prompts = [f"question {i}" for i in range(1, 6)]
    for prompt in prompts:
        manager.store_cache(prompt, "gpt-4", f"answer for {prompt}")
        clock.stamp(entry(manager, prompt))
        clock.advance(60)

    assert manager.cache.count() == 3
    assert manager.check_cache(prompts[4], "gpt-4") == "answer for question 5"
    assert manager.check_cache(prompts[3], "gpt-4") == "answer for question 4"
    assert manager.check_cache(prompts[0], "gpt-4") is None      # the oldest went
    assert manager.check_cache(prompts[1], "gpt-4") is None
    assert manager.get_stats()["cache_pruned"] == 2
    assert not entry(manager, prompts[0]).exists()


def test_a_write_under_the_cap_triggers_no_sweep_of_its_own(tmp_path):
    """The cap costs a filename count, not a folder walk, until it is reached."""
    manager, spy = _manager_over(SpyCache(tmp_path / "c"), cache_max_entries=500)
    manager.store_cache("p", "gpt-4", "answer")
    assert spy.pruned_with == []


def test_the_sweep_happens_when_writing_and_never_when_reading(tmp_path):
    """A read path that sweeps the folder makes the user wait for disk work they
    did not ask for, so the write is the only one that pays for it."""
    manager, spy = _manager_over(SpyCache(tmp_path / "c"), cache_max_entries=2)
    manager.store_cache("p", "gpt-4", "answer")
    assert spy.pruned_with == []

    for i in range(5):                      # the folder is over the cap now
        spy.entries[f"old{i}"] = "stale"
    manager.store_cache("p2", "gpt-4", "answer2")
    assert spy.pruned_with == [2]

    manager.check_cache("p2", "gpt-4")
    manager.check_cache("nobody asked", "gpt-4")
    assert spy.pruned_with == [2]           # ... and nothing swept on the reads


def test_the_cap_is_at_least_one_entry(tmp_path):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_max_entries=0)
    assert manager.cache_max_entries == 1


def test_a_read_never_sweeps_the_folder(tmp_path, clock):
    """Pruning on the read path makes the user wait for disk work they did not
    ask for, so it is the write that pays."""
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"),
                             cache_max_entries=2)
    for i in range(4):
        manager.store_cache(f"p{i}", "gpt-4", f"a{i}")

    def no_sweep(**kw):
        raise AssertionError("the read path swept the cache folder")

    manager.cache.prune = no_sweep
    assert manager.check_cache("p3", "gpt-4") == "a3"
    assert manager.check_cache("never asked", "gpt-4") is None


# --- a cache that broke -----------------------------------------------------

def test_a_half_written_entry_is_a_miss_and_does_not_raise(tmp_path, clock):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", "answer")
    entry(manager, "p").write_text('{"saved_at": 123, "response": "cut off mid',
                                   encoding="utf-8")
    assert manager.check_cache("p", "gpt-4") is None


def test_an_entry_that_cannot_be_read_is_a_miss_said_once(tmp_path, clock):
    """Something else owns the file name — a folder, a root squash, a disk going
    to sleep. The turn loses an answer, not the session."""
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", "answer")
    path = entry(manager, "p")
    path.unlink()
    path.mkdir()
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            assert manager.check_cache("p", "gpt-4") is None
            assert manager.check_cache("p", "gpt-4") is None
            assert manager.check_cache("p", "gpt-4") is None
    finally:
        path.rmdir()
    assert len(caught) == 1
    assert manager.cache_failure


def test_a_read_that_raises_from_the_cache_is_still_only_a_miss(tmp_path, clock):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))

    def broken(prompt, model):
        raise OSError("the drive is gone")

    manager.cache.get = broken
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        assert manager.check_cache("p", "gpt-4") is None
        assert manager.check_cache("p", "gpt-4") is None
        assert manager.check_cache("p", "gpt-4") is None
    assert len(caught) == 1
    assert "economy cache" in str(caught[0].message)
    assert "the drive is gone" in manager.cache_failure


def test_a_write_that_raises_does_not_cost_the_turn(tmp_path, clock):
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))

    def broken(prompt, model, response):
        raise OSError("no space left on device")

    manager.cache.store = broken
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        for _ in range(3):
            manager.store_cache("p", "gpt-4", "answer")
    assert len(caught) == 1
    assert manager.check_cache("p", "gpt-4") is None


# --- the folder itself ------------------------------------------------------

def test_the_cache_directory_is_created_when_missing(tmp_path):
    nested = tmp_path / "deep" / "nest" / "cache"
    assert not nested.exists()
    manager = EconomyManager(mode="economy", cache_dir=str(nested))
    assert nested.is_dir()


def test_a_folder_deleted_under_the_cache_is_remade_on_the_next_write(tmp_path, clock):
    """`git clean -fdx` and the phone's cleaner both take the folder away mid-run."""
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", "answer")
    shutil.rmtree(tmp_path / "c")
    assert manager.check_cache("p", "gpt-4") is None

    manager.store_cache("p2", "gpt-4", "answer2")
    assert (tmp_path / "c").is_dir()
    assert manager.check_cache("p2", "gpt-4") == "answer2"


def test_normal_mode_creates_nothing(tmp_path):
    manager = EconomyManager(mode="normal", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", "answer")
    assert manager.check_cache("p", "gpt-4") is None
    assert not (tmp_path / "c").exists()


# --- the contract from test_permissions.py, seen from this side -------------

def test_the_manager_hands_back_exactly_what_the_folder_holds(tmp_path, clock):
    """A cached tool call is refused by the loop, so the cache must not lie about
    what it read. The replay rule itself is pinned in
    tests/test_permissions.py::test_a_cached_tool_call_is_never_replayed.
    """
    tool_call = '{"tool": "recorder", "args": {"note": "from cache"}}'
    manager = EconomyManager(mode="economy", cache_dir=str(tmp_path / "c"))
    manager.store_cache("p", "gpt-4", tool_call)
    assert manager.check_cache("p", "gpt-4") == tool_call
    assert CommandParser().parse(tool_call).has_commands is True
