"""Economy mode: an identical prompt gets back the answer that was paid for.

The cache is bounded twice, and both bounds are enforced where entries are made
— on the write:

* `cache_ttl_minutes` decides how long an answer may stand, so a reply about a
  file that changed since is not served forever;
* `cache_max_entries` decides how many files the folder may hold. An answer is a
  few kilobytes, so five hundred of them are a couple of megabytes — which is
  the difference between "it works on my laptop" and "it works on the phone in
  my pocket", where the free tier actually runs.

Nothing sweeps the folder on a read: a hit costs one file read, and the user
never waits for disk work they did not ask for. Expired entries that no one
asks about again are collected by the same write-time sweep, and in the meantime
`get` refuses to serve them, so the cap bounds the disk and the ttl bounds the
lie.
"""
import warnings
from pathlib import Path

from beeagent.utils.cache import ResponseCache

# The one entry count the manager holds itself to. There is no config key for
# it on purpose: `economy.cache_max_entries` in a file most users never open
# would be a second way to leave the folder unbounded.
DEFAULT_MAX_ENTRIES = 500

# What a broken cache raises on an ordinary machine: an unreadable or
# half-written entry, a folder deleted underneath us, a file shape we never
# wrote. None of them is a reason to lose the turn.
CACHE_FAILURES = (OSError, ValueError, TypeError)


class EconomyManager:
    """Answer caching for `--mode economy`.

    Only final plain-text answers are cached: a cached tool call must never be
    replayed, or the loop would print the JSON instead of running it. That rule
    is enforced in the loop itself (`agent._carries_a_call`) rather than here, so
    it still holds for a caller that swaps `check_cache` out — this class only
    ever hands back what the folder says.
    """

    def __init__(self, mode: str = "normal", cache_dir: str = ".beeagent/cache",
                 cache_enabled: bool = True, cache_ttl_minutes: int = 30,
                 cache_max_entries: int = DEFAULT_MAX_ENTRIES):
        self.mode = mode
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir
        self.cache_ttl_minutes = cache_ttl_minutes
        # A cap of zero would delete every answer the moment it is written; one
        # is the smallest folder that still answers its own last question.
        self.cache_max_entries = max(1, int(cache_max_entries))
        self.request_count = 0
        self.pruned_entries = 0
        self._cache: ResponseCache | None = None
        self._cache_failure = ""
        self._cache_warned = False
        self.cache = self._open_cache()

    # --- the cache object ---------------------------------------------------

    @property
    def cache(self) -> ResponseCache | None:
        return self._cache

    @cache.setter
    def cache(self, cache: ResponseCache | None):
        """Assigning a cache cannot opt out of the configured freshness bound.

        `/mode economy` hands the manager a cache it built itself, without the
        ttl from the config file; the ttl belongs to the mode, not to whoever
        happened to open the folder.
        """
        if cache is not None:
            cache.ttl_seconds = self.cache_ttl_minutes * 60
        self._cache = cache

    def _open_cache(self) -> ResponseCache | None:
        if not self.should_cache():
            return None
        try:
            return ResponseCache(self.cache_dir, ttl_seconds=self.cache_ttl_minutes * 60)
        except CACHE_FAILURES as exc:          # a folder that cannot be made
            self._note_cache_failure("open", exc)
            return None

    def should_cache(self) -> bool:
        return self.mode == "economy" and self.cache_enabled

    def set_mode(self, mode: str):
        self.mode = mode
        self.cache = self._open_cache()

    # --- the two paths ------------------------------------------------------

    def check_cache(self, prompt: str, model: str) -> str | None:
        """One file read. Never a sweep — see the module docstring."""
        cache = self.cache
        if cache is None:
            return None
        try:
            return cache.get(prompt, model)
        except CACHE_FAILURES as exc:
            self._note_cache_failure("read", exc)
            return None

    def store_cache(self, prompt: str, model: str, response: str):
        """Write the answer, then bring the folder back inside both bounds.

        The cap is the manager's own business, not a favour the cache object may
        or may not do for itself: an entry that is only ever deleted by the one
        prompt that would read it again leaves every question asked once on disk
        for the life of the project.
        """
        cache = self.cache
        if cache is None:
            return
        try:
            Path(cache.cache_dir).mkdir(parents=True, exist_ok=True)
            cache.store(prompt, model, response)
            self._prune_to_cap(cache)
        except CACHE_FAILURES as exc:
            self._note_cache_failure("write", exc)

    def _prune_to_cap(self, cache: ResponseCache) -> int:
        """Drop expired entries, then the oldest past the cap.

        Cheap by construction: the folder is only walked once it is actually
        over the limit, so a session that asks thirty questions never pays for a
        sweep at all.
        """
        if cache.count() <= self.cache_max_entries:
            return 0
        removed = cache.prune(keep=self.cache_max_entries)
        self.pruned_entries += removed
        return removed

    # --- what the user is told ----------------------------------------------

    def _note_cache_failure(self, what: str, exc: Exception):
        """A cache that broke is a miss, not a lost turn.

        Said once per session: the tenth request would report the same unreadable
        folder, and economy mode has no right to interrupt the user for its own
        bookkeeping every prompt.
        """
        self._cache_failure = f"{what}: {exc}"
        if not self._cache_warned:
            self._cache_warned = True
            warnings.warn(f"economy cache is being ignored ({self._cache_failure})",
                          RuntimeWarning, stacklevel=3)

    @property
    def cache_failure(self) -> str:
        """The first cache error of the session, empty when there was none."""
        return self._cache_failure

    def get_stats(self) -> dict:
        cache = self.cache
        try:
            cached = cache.count() if cache else 0
        except CACHE_FAILURES:
            cached = 0
        return {
            "mode": self.mode,
            "requests": self.request_count,
            "cache": "on" if cache else "off",
            "cached_answers": cached,
            "cache_ttl_minutes": self.cache_ttl_minutes,
            "cache_max_entries": self.cache_max_entries,
            "cache_pruned": self.pruned_entries,
        }
