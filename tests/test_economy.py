import json

from beeagent.core.economy import EconomyManager
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
