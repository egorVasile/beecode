"""Free-tier LLM endpoints that speak the OpenAI API.

Every entry needs a key **issued to you by the service itself** — sign up, copy
the key, store it with `/key <name> <token>` (or export the env var listed
here). BeeCode never ships, harvests or shares other people's keys: that is
stolen access to their accounts and it gets everyone banned.

`free` says what the provider publicly offers without paying; verify on the
signup page, quotas move around.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Endpoint:
    name: str            # used as the provider name: /provider <name>
    label: str           # shown in /providers
    url: str             # OpenAI-compatible base url (without /chat/completions)
    env: str             # environment variable the key is read from
    signup: str          # where to get your own key
    free: str            # what the free tier gives
    models: tuple[str, ...] = ()   # defaults; /models asks the endpoint live


ENDPOINTS = (
    Endpoint(
        name="openrouter", label="OpenRouter",
        url="https://openrouter.ai/api/v1", env="OPENROUTER_API_KEY",
        signup="https://openrouter.ai/settings/keys",
        free="a shelf of `:free` models, no card needed",
        models=("meta-llama/llama-3.3-70b-instruct:free",
                "deepseek/deepseek-chat-v3-0324:free",
                "qwen/qwen3-235b-a22b:free"),
    ),
    Endpoint(
        name="groq", label="Groq",
        url="https://api.groq.com/openai/v1", env="GROQ_API_KEY",
        signup="https://console.groq.com/keys",
        free="very fast llama/qwen models with a generous free quota",
        models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen3-32b"),
    ),
    Endpoint(
        name="gemini", label="Google AI Studio",
        url="https://generativelanguage.googleapis.com/v1beta/openai",
        env="GEMINI_API_KEY",
        signup="https://aistudio.google.com/apikey",
        free="flash/mini models free per minute, no card",
        models=("gemini-2.5-flash", "gemini-2.5-flash-lite", "gemini-2.0-flash"),
    ),
    Endpoint(
        name="huggingface", label="Hugging Face",
        url="https://router.huggingface.co/v1", env="HF_TOKEN",
        signup="https://huggingface.co/settings/tokens",
        free="free inference credits for a rotating set of open models",
        models=("meta-llama/Llama-3.3-70B-Instruct", "deepseek-ai/DeepSeek-V3"),
    ),
    Endpoint(
        name="github", label="GitHub Models",
        url="https://models.inference.ai.azure.com", env="GITHUB_TOKEN",
        signup="https://github.com/settings/tokens (model inference scope)",
        free="gpt-4o/llama endpoints free for your own GitHub account",
        models=("gpt-4o", "Meta-Llama-3.3-70B-Instruct", "Mistral-large"),
    ),
    Endpoint(
        name="cerebras", label="Cerebras Cloud",
        url="https://api.cerebras.ai/v1", env="CEREBRAS_API_KEY",
        signup="https://cloud.cerebras.ai",
        free="qwen/llama at very high speed, free tier",
        models=("llama3.1-8b", "qwen-3-32b"),
    ),
    Endpoint(
        name="mistral", label="Mistral La Plateforme",
        url="https://api.mistral.ai/v1", env="MISTRAL_API_KEY",
        signup="https://console.mistral.ai",
        free="experiment tier: small models, rate limited, no card",
        models=("mistral-small-latest", "open-mistral-nemo"),
    ),
    Endpoint(
        name="together", label="Together AI",
        url="https://api.together.xyz/v1", env="TOGETHER_API_KEY",
        signup="https://api.together.xyz/settings/api-keys",
        free="a one-time credit; some open models stay free",
        models=("meta-llama/Llama-3.3-70B-Instruct-Turbo-Free",),
    ),
)


BY_NAME = {e.name: e for e in ENDPOINTS}


def key_for(endpoint: Endpoint, api_keys: dict | None = None) -> str:
    """The user's own key: config first, then the documented env var."""
    import os

    value = (api_keys or {}).get(endpoint.name) or os.environ.get(endpoint.env, "")
    return value.strip()
