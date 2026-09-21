"""On-disk answer cache for economy mode.

Entries expire (`ttl_seconds`) because a reply about a file is not true once
that file changes, and only the hash of the prompt is kept — the prompt itself
carries quoted file contents and tool output, which has no business being
written next to the project.
"""
import hashlib
import json
import time
from pathlib import Path


class ResponseCache:
    def __init__(self, cache_dir: str = ".beeagent/cache", ttl_seconds: float = 30 * 60):
        self.cache_dir = Path(cache_dir)
        self.ttl_seconds = ttl_seconds
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _key(self, prompt: str, model: str) -> str:
        data = f"{prompt}|||{model}"
        return hashlib.sha256(data.encode("utf-8")).hexdigest()

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def get(self, prompt: str, model: str) -> str | None:
        path = self._path(self._key(prompt, model))
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            return None
        if time.time() - float(data.get("saved_at", 0)) > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return data.get("response")

    def store(self, prompt: str, model: str, response: str):
        path = self._path(self._key(prompt, model))
        payload = {"saved_at": time.time(), "model": model, "response": response}
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def count(self) -> int:
        return sum(1 for _ in self.cache_dir.glob("*.json"))
