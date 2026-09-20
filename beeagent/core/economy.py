from beeagent.utils.cache import ResponseCache


class EconomyManager:
    """Answer caching for `--mode economy`.

    Only final plain-text answers are cached: a cached tool call must never be
    replayed, or the loop would print the JSON instead of running it.
    """

    def __init__(self, mode: str = "normal", cache_dir: str = ".beeagent/cache",
                 cache_enabled: bool = True, cache_ttl_minutes: int = 30):
        self.mode = mode
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir
        self.cache_ttl_minutes = cache_ttl_minutes
        self.cache = self._open_cache()
        self.request_count = 0

    def _open_cache(self) -> ResponseCache | None:
        if not self.should_cache():
            return None
        return ResponseCache(self.cache_dir, ttl_seconds=self.cache_ttl_minutes * 60)

    def should_cache(self) -> bool:
        return self.mode == "economy" and self.cache_enabled

    def set_mode(self, mode: str):
        self.mode = mode
        self.cache = self._open_cache()

    def check_cache(self, prompt: str, model: str) -> str | None:
        if self.cache:
            return self.cache.get(prompt, model)
        return None

    def store_cache(self, prompt: str, model: str, response: str):
        if self.cache:
            self.cache.store(prompt, model, response)

    def get_stats(self) -> dict:
        return {
            "mode": self.mode,
            "requests": self.request_count,
            "cache": "on" if self.cache else "off",
            "cached_answers": self.cache.count() if self.cache else 0,
        }
