from pydantic import BaseModel, Field
from typing import Optional

class EconomyConfig(BaseModel):
    cache_enabled: bool = True
    cache_dir: str = ".beeagent/cache"
    # A cached answer is only valid for as long as the files behind it are
    # assumed unchanged; after that it is re-asked.
    cache_ttl_minutes: int = 30

class PermissionsConfig(BaseModel):
    # ask: unsafe tools need /allow first · auto: run anything · readonly: read-only
    mode: str = "ask"
    allowed: list[str] = Field(default_factory=list)

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
    # 0 = take the window from the model; otherwise a ceiling in tokens.
    max_context_tokens: int = 0
    # How long a silent endpoint may hold a request before we give up on the
    # attempt and retry. Free providers stall regularly; without this the agent
    # waits forever and looks hung.
    stream_idle_timeout: int = 90
    custom_providers: list[CustomProvider] = Field(default_factory=list)
    # Keys the user obtained themselves, by provider name (see /providers).
    api_keys: dict[str, str] = Field(default_factory=dict)
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
