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

    @staticmethod
    def _text(value) -> str:
        # Callers pass model output and session rows; None or a non-string used
        # to die in len() with TypeError instead of a cache miss/store.
        if isinstance(value, str):
            return value
        return str(value) if value is not None else ""

    def _key(self, prompt: str, model: str) -> str:
        # Length-prefixed: "a"+"|||"+"m|||b" and "a|||m"+"|||"+"b" hashed the
        # same and one prompt read another's answer. Lengths disambiguate.
        prompt, model = self._text(prompt), self._text(model)
        data = f"{len(prompt)}\n{prompt}\n{len(model)}\n{model}"
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
        # A bytes/dict answer used to die in json.dumps with TypeError instead
        # of being cached. Text is cached verbatim; anything else as its str().
        if not isinstance(response, str):
            try:
                json.dumps(response)
            except (TypeError, ValueError):
                response = str(response)
        path = self._path(self._key(prompt, model))
        payload = {"saved_at": time.time(), "model": model, "response": response}
        # Unique temp: two processes caching the same prompt shared one
        # ".json.tmp" and interleaved writes.
        import tempfile
        descriptor, tmp_name = tempfile.mkstemp(dir=str(self.cache_dir),
                                                prefix=".cache-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False))
            os.replace(tmp_name, path)
        finally:
            try:
                if os.path.exists(tmp_name):
                    os.remove(tmp_name)
            except OSError:
                pass
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
                mtime = candidate.stat().st_mtime
            except OSError:
                continue
            # Expiry is decided by saved_at inside the entry (what get()
            # enforces), not by mtime: a touch resurrected the expired. mtime
            # stays as the tie-break below, so equal saved_at evict oldest
            # first instead of whatever order the directory lists.
            try:
                saved_at = float(json.loads(
                    candidate.read_text(encoding="utf-8")).get("saved_at", 0))
            except (OSError, ValueError, TypeError, AttributeError):
                saved_at = 0
            entries.append((saved_at, mtime, candidate))
        for saved_at, _mtime, candidate in entries:
            if now - saved_at > self.ttl_seconds:
                candidate.unlink(missing_ok=True)
                removed += 1
        if len(entries) - removed > keep:
            survivors = sorted(((s, m, c) for s, m, c in entries if c.exists()))
            for _, _, candidate in survivors[:len(survivors) - keep]:
                candidate.unlink(missing_ok=True)
                removed += 1
        return removed

    def count(self) -> int:
        return sum(1 for _ in self.cache_dir.glob("*.json"))
