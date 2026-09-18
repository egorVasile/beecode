import hashlib
import json
from pathlib import Path

class ResponseCache:
    def __init__(self, cache_dir: str = ".beeagent/cache"):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
    
    def _key(self, prompt: str, model: str) -> str:
        data = f"{prompt}|||{model}"
        return hashlib.sha256(data.encode()).hexdigest()
    
    def get(self, prompt: str, model: str) -> str | None:
        key = self._key(prompt, model)
        path = self.cache_dir / f"{key}.json"
        if path.exists():
            data = json.loads(path.read_text())
            return data.get("response")
        return None
    
    def store(self, prompt: str, model: str, response: str):
        key = self._key(prompt, model)
        path = self.cache_dir / f"{key}.json"
        path.write_text(json.dumps({
            "prompt": prompt[:100],
            "model": model,
            "response": response,
        }))
