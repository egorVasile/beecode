"""On-disk answer cache for economy mode.

Entries expire (`ttl_seconds`) because a reply about a file is not true once
that file changes, and only the hash of the prompt is kept — the prompt itself
carries quoted file contents and tool output, which has no business being
written next to the project.
"""
import hashlib
import json
import os
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
            # A file that parses but is not the shape we wrote — a list, a bare
            # string, `saved_at` of "soon" — is a broken entry, not a crash.
            if not isinstance(data, dict):
                raise ValueError("not an object")
            saved_at = float(data.get("saved_at", 0))
            answer = data.get("response")
            if not isinstance(answer, str):
                raise ValueError("no answer in it")
        except (OSError, ValueError, TypeError):
            path.unlink(missing_ok=True)
            return None
        if time.time() - saved_at > self.ttl_seconds:
            path.unlink(missing_ok=True)
            return None
        return answer

    def store(self, prompt: str, model: str, response: str):
        path = self._path(self._key(prompt, model))
        payload = {"saved_at": time.time(), "model": model, "response": response}
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self.prune()

    def prune(self, keep: int = 500) -> int:
        """Drop what has expired, then the oldest beyond `keep`.

        An entry is only ever deleted by the one prompt that would read it again,
        so a question asked once left its file for the lifetime of the project.
        """
        if not self.cache_dir.is_dir():
            return 0
        now = time.time()
        entries = []
        removed = 0
        for candidate in self.cache_dir.glob("*.json"):
            try:
                entries.append((candidate.stat().st_mtime, candidate))
            except OSError:
                continue
        for stamp, candidate in entries:
            if now - stamp > self.ttl_seconds:
                candidate.unlink(missing_ok=True)
                removed += 1
        if len(entries) - removed > keep:
            survivors = sorted((s, c) for s, c in entries if c.exists())
            for _, candidate in survivors[:len(survivors) - keep]:
                candidate.unlink(missing_ok=True)
                removed += 1
        return removed

    def count(self) -> int:
        return sum(1 for _ in self.cache_dir.glob("*.json"))
