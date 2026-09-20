from pydantic import BaseModel, Field
from typing import Optional

class EconomyConfig(BaseModel):
    cache_enabled: bool = True
    batch_tools: bool = True
    smart_routing: bool = True
    cache_dir: str = ".beeagent/cache"

class CustomProvider(BaseModel):
    name: str
    type: str  # "openai_compat" | "ollama"
    url: str
    model: str
    key: Optional[str] = None

class BeeConfig(BaseModel):
    model: str = "gpt-4"
    provider: str = "g4f"
    mode: str = "normal"  # "normal" | "economy"
    language: str = "en"  # "en" | "ru"
    max_turns: int = 50
    custom_providers: list[CustomProvider] = Field(default_factory=list)
    # Keys the user obtained themselves, by provider name (see /providers).
    api_keys: dict[str, str] = Field(default_factory=dict)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
