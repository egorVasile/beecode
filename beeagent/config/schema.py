"""What `beeagent.json` holds. The rules of typing live in `model.py`.

Nothing in here imports pydantic: on Android pip has no wheel for its Rust core
and the phone has no compiler, and this file is the reason the program starts.
"""
from typing import Optional

from .model import Model, default


class EconomyConfig(Model):
    cache_enabled: bool = True
    cache_dir: str = ".beeagent/cache"
    # A cached answer is only valid for as long as the files behind it are
    # assumed unchanged; after that it is re-asked.
    cache_ttl_minutes: int = 30


class PermissionsConfig(Model):
    # ask: unsafe tools need /allow first · auto: run anything · readonly: read-only
    mode: str = "ask"
    allowed: list[str] = default(list)


class CustomProvider(Model):
    # All four are required: an entry that names a provider without saying what
    # it is or where it lives is a typo, and a typo here means the config file
    # is refused and set aside, which is the honest answer.
    name: str
    type: str        # "openai_compat" | "ollama"
    url: str
    model: str
    key: Optional[str] = None


class BeeConfig(Model):
    # Command A is the one g4f route measured (2026-09-21) to read a 32k
    # prompt keyless and answer in seconds; "gpt-4" landed on a route that
    # refuses anything past ~4k, which is what made the agent seem amnesic.
    model: str = "command-a-03-2025"
    provider: str = "g4f"
    mode: str = "normal"  # "normal" | "economy"
    # Endpoints that accept tool calls as data (OpenAI-shaped `tools`) are
    # asked that way: no JSON in prose, no catalog in the prompt, nothing for
    # the text parser to repair. Turn it off to force the old protocol.
    native_tools: bool = True
    language: str = "en"  # "en" | "ru"
    max_turns: int = 50
    # 0 = take the window from the model; otherwise a ceiling in tokens.
    max_context_tokens: int = 0
    # How long a silent endpoint may hold a request before we give up on the
    # attempt and retry. Free providers stall regularly; without this the agent
    # waits forever and looks hung.
    stream_idle_timeout: int = 90
    custom_providers: list[CustomProvider] = default(list)
    # Keys the user obtained themselves, by provider name (see /providers).
    api_keys: dict[str, str] = default(dict)
    # A pool the operator runs: the address of the proxy and the seat token it
    # gave this install. No API key lives here — that is the point of the pool.
    #
    # The address ships, because an install that has to be told where the pool is
    # is an install where nothing works until someone types two commands. An
    # address is not a secret and not a credential: it is the door, and the three
    # seats per address a day, the 25 a day overall and the per-install signature
    # are the lock.
    pool_url: str = "https://beecode-pool.onrender.com"
    pool_token: str = ""
    # A command that changes this machine's exit address (a VPN client's CLI).
    # BeeCode only runs it after the user pressed "yes", and shows it verbatim.
    vpn_command: str = ""
    permissions: PermissionsConfig = default(PermissionsConfig)
    economy: EconomyConfig = default(EconomyConfig)
    # Interface slots: frame / banner / spinner / stream. "none" is a valid
    # choice for any of them — see /skin.
    ui: dict[str, str] = default(dict)
    # The programmable skin to wear from the first frame (`/skins <name>` writes
    # it). Empty means BeeCode's own interface, which is also the safe value: a
    # skin that is not installed is skipped at startup, not an error.
    skin: str = ""
    # Settings owned by plugins, by plugin name: {"word-count": {"limit": 20}}.
    extensions: dict[str, dict] = default(dict)
