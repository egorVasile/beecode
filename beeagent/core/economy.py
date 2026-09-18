from beeagent.utils.cache import ResponseCache

class EconomyManager:
    def __init__(self, mode: str = "normal", cache_dir: str = ".beeagent/cache"):
        self.mode = mode
        self.cache = ResponseCache(cache_dir) if mode == "economy" else None
        self.request_count = 0
        self.tokens_saved = 0
    
    def should_cache(self) -> bool:
        return self.mode == "economy" and self.cache is not None
    
    def should_batch(self) -> bool:
        return self.mode == "economy"
    
    def should_smart_route(self) -> bool:
        return self.mode == "economy"
    
    def check_cache(self, prompt: str, model: str) -> str | None:
        if self.should_cache() and self.cache:
            return self.cache.get(prompt, model)
        return None
    
    def store_cache(self, prompt: str, model: str, response: str):
        if self.should_cache() and self.cache:
            self.cache.store(prompt, model, response)
    
    def select_model(self, task_type: str, default_model: str) -> str:
        if not self.should_smart_route():
            return default_model
        cheap_models = ["gpt-3.5-turbo", "llama-3.1-8b", "qwen-7b"]
        if task_type in ("grep", "glob", "read", "todo"):
            return cheap_models[0]
        return default_model
    
    def get_stats(self) -> dict:
        return {
            "mode": self.mode,
            "requests": self.request_count,
            "tokens_saved": self.tokens_saved,
        }
