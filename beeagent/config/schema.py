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
    # Command A is the one g4f route measured (2026-09-21) to read a 32k
    # prompt keyless and answer in seconds; "gpt-4" landed on a route that
    # refuses anything past ~4k, which is what made the agent seem amnesic.
    model: str = "command-a-03-2025"
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
    # A pool the operator runs: the address of the proxy and the seat token it
    # gave this install. No API key lives here — that is the point of the pool.
    pool_url: str = ""
    pool_token: str = ""
    permissions: PermissionsConfig = Field(default_factory=PermissionsConfig)
    economy: EconomyConfig = Field(default_factory=EconomyConfig)
    # Interface slots: frame / banner / spinner / stream. "none" is a valid
    # choice for any of them — see /skin.
    ui: dict[str, str] = Field(default_factory=dict)
    # Settings owned by plugins, by plugin name: {"word-count": {"limit": 20}}.
    extensions: dict[str, dict] = Field(default_factory=dict)
