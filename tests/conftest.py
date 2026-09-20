"""Shared test hygiene.

Measured context windows live in a project-local cache. Left alone, a value
written by one test (or sitting in the working tree from a real measurement)
would silently change what other tests believe a model's window is.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolated_window_cache(tmp_path, monkeypatch):
    from beeagent.core import windows

    monkeypatch.setattr(windows, "CACHE", tmp_path / ".beeagent" / "windows.json")
