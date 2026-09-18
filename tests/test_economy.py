import os
import shutil
from beeagent.utils.cache import ResponseCache
from beeagent.core.economy import EconomyManager

def test_cache_store_and_retrieve(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("hello", "gpt-4", "world")
    result = cache.get("hello", "gpt-4")
    assert result == "world"

def test_cache_miss(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    result = cache.get("nonexistent", "gpt-4")
    assert result is None

def test_cache_different_models(tmp_path):
    cache = ResponseCache(cache_dir=str(tmp_path))
    cache.store("prompt", "gpt-4", "response4")
    cache.store("prompt", "gpt-3.5", "response35")
    assert cache.get("prompt", "gpt-4") == "response4"
    assert cache.get("prompt", "gpt-3.5") == "response35"

def test_economy_manager_normal():
    manager = EconomyManager(mode="normal")
    assert manager.should_cache() is False
    assert manager.should_batch() is False
    assert manager.should_smart_route() is False

def test_economy_manager_economy():
    manager = EconomyManager(mode="economy")
    assert manager.should_cache() is True
    assert manager.should_batch() is True
    assert manager.should_smart_route() is True

def test_economy_smart_routing():
    manager = EconomyManager(mode="economy")
    model = manager.select_model("grep", "gpt-4")
    assert model != "gpt-4"  # Should route to cheaper model
    model = manager.select_model("codegen", "gpt-4")
    assert model == "gpt-4"  # Should keep expensive model

def test_economy_stats():
    manager = EconomyManager(mode="economy")
    stats = manager.get_stats()
    assert stats["mode"] == "economy"
    assert stats["requests"] == 0
