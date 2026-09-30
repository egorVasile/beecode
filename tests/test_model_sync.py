"""The startup catalogue check: `beeagent/core/model_sync.py`.

Every start asks every provider what it serves, diffs against the last saved
list, and reports additions and removals per provider. A provider that will
not answer is skipped, never fatal — the catalogue moves without a release,
but a hanging start would be worse than a stale list.
"""
import json

import pytest

from beeagent.core import model_sync
from beeagent.core.model_sync import ModelDiff
from beeagent.providers.registry import ProviderRegistry


class FakeProvider:
    def __init__(self, name, models, live=None, fail=False):
        self.name = name
        self.models = list(models)
        self._live = live
        self._fail = fail

    def discover_models(self):
        if self._fail:
            raise RuntimeError("the endpoint is down")
        live = self._live if self._live is not None else self.models
        return list(live)


class FakeAgent:
    def __init__(self, providers):
        self.providers = ProviderRegistry()
        for provider in providers:
            self.providers.register(provider)


def _cache(tmp_path, name, models):
    path = tmp_path / ".beeagent" / f"models_{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"saved_at": 9999999999, "version": "x",
                                "models": models}), encoding="utf-8")


def test_first_sight_writes_baseline_and_reports_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    agent = FakeAgent([FakeProvider("crax", ["a", "b"])])
    assert model_sync.check_all(agent, str(tmp_path)) == []
    assert model_sync._read_cache(str(tmp_path), "crax") == ["a", "b"]


def test_changed_catalogue_reports_added_and_removed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path, "crax", ["a", "b", "c"])
    agent = FakeAgent([FakeProvider("crax", ["a"], live=["a", "d"])])
    diffs = model_sync.check_all(agent, str(tmp_path))
    assert len(diffs) == 1
    diff = diffs[0]
    assert diff.provider == "crax" and diff.count == 2
    assert diff.added == ["d"] and diff.removed == ["b", "c"]


def test_unchanged_catalogue_is_silence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path, "crax", ["a", "b"])
    agent = FakeAgent([FakeProvider("crax", ["a", "b"])])
    assert model_sync.check_all(agent, str(tmp_path)) == []


def test_a_provider_that_will_not_answer_is_skipped(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path, "dead", ["a"])
    agent = FakeAgent([FakeProvider("dead", ["a"], fail=True)])
    assert model_sync.check_all(agent, str(tmp_path)) == []


def test_opt_out_env_skips_everything(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BEECODE_MODEL_SYNC", "0")
    _cache(tmp_path, "crax", ["a"])
    agent = FakeAgent([FakeProvider("crax", ["a"], live=["b"])])
    assert model_sync.check_all(agent, str(tmp_path)) == []


def test_report_counts_and_names(tmp_path):
    diff = ModelDiff(provider="crax", added=["x", "y"], removed=["z"], count=5)
    text = model_sync.report([diff])
    assert "crax" in text and "5" in text
    assert "+2" in text and "-1" in text
    assert "x" in text and "z" in text


def _tty(monkeypatch):
    import sys

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_startup_check_saves_on_yes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _cache(tmp_path, "crax", ["a"])
    agent = FakeAgent([FakeProvider("crax", ["a"], live=["a", "b"])])
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    model_sync.startup_check(agent, str(tmp_path), interactive=True)
    assert model_sync._read_cache(str(tmp_path), "crax") == ["a", "b"]


def test_startup_check_keeps_on_no(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _tty(monkeypatch)
    _cache(tmp_path, "crax", ["a"])
    agent = FakeAgent([FakeProvider("crax", ["a"], live=["a", "b"])])
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    model_sync.startup_check(agent, str(tmp_path), interactive=True)
    assert model_sync._read_cache(str(tmp_path), "crax") == ["a"]


def test_startup_check_without_terminal_reports_and_saves(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    _cache(tmp_path, "crax", ["a"])
    agent = FakeAgent([FakeProvider("crax", ["a"], live=["a", "b"])])
    model_sync.startup_check(agent, str(tmp_path), interactive=True)
    assert model_sync._read_cache(str(tmp_path), "crax") == ["a", "b"]
    assert "crax" in capsys.readouterr().out


def test_modeldiff_changed_property():
    assert ModelDiff(provider="x").changed is False
    assert ModelDiff(provider="x", added=["a"]).changed is True
    assert ModelDiff(provider="x", removed=["a"]).changed is True
