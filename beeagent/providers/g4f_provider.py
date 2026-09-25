"""g4f provider restricted to vetted keyless providers.

g4f's auto-routing is deliberately not used: it chooses from the whole installed
catalogue, and parts of that catalogue reach their endpoint by driving a headless
browser through a bot check. BeeCode must not ship that as a fallback, so when
the pinned providers below are exhausted this raises and the agent moves on to
the pool or a `/key` endpoint.

Streaming yields (kind, text) tuples:
  ("content",   str)  -- the answer itself
  ("reasoning", str)  -- thinking-block tokens (model-dependent)
"""
from typing import AsyncIterator
import inspect

from .base import BaseProvider


# Keyless providers, fastest and largest-prompt first. Measured 2026-09-21
# against g4f 8.5.7: LLM7 and CohereForAI answered a 4k-token prompt with the
# needle intact in under 2 seconds.
#
# Two names were dropped from the old list. "Cloudflare" is not an API client:
# g4f/Provider/Cloudflare.py:34 drives a headless Chrome at
# playground.ai.cloudflare.com through g4f/requests/cdp.py, which defines
# click_turnstile_checkbox() (:833) and bypass_turnstile() (:1080) — BeeCode
# must not ship a route that solves a bot check for its users. "Yqcloud" posts
# to api.binjie.fun with spoofed browser origin/referer/UA headers, an
# undocumented relay whose upstream model provenance nobody states.
#
# "KiloCode" joined on 2026-09-24 after reading its own source, not just its
# label: g4f/Provider/__init__.py:335 builds it through
# client/factory.py:create_custom_provider, which is `OpenaiTemplate` with
# base_url https://api.kilo.ai/api/gateway, `api_key = None`, `needs_auth =
# False` and headers of exactly {"Content-Type": "application/json"}. That
# template posts with g4f's aiohttp StreamSession and `impersonate=None`
# (Provider/template/OpenaiTemplate.py:249-252), so the Chrome UA/`sec-ch-ua`
# set in requests/defaults.py is never applied — no browser fingerprint, no
# cookie, no Turnstile, no proof-of-work, and curl_cffi/playwright/nodriver are
# not imported by the path a request takes. It is a plain HTTPS call.
#
# The list has rotted before: it once named Free2GPT, Blackbox and DDG, none of
# which exist in this g4f any more, so the "keyless fallback" had quietly
# shrunk to two providers before auto-routing took over and picked upstreams
# that demand a key. tests/test_providers.py fails if a name here stops
# resolving, which is the only reason the rot was ever noticed.
KEYLESS_PROVIDERS = ("LLM7", "CohereForAI_C4AI_Command", "KiloCode")


def _keyless_providers() -> list:
    """Resolve known keyless provider classes, skipping unavailable ones."""
    providers = []
    try:
        import g4f.Provider as P
        for name in KEYLESS_PROVIDERS:
            try:
                cls = getattr(P, name)
            except Exception:
                continue
            if getattr(cls, "working", False):
                providers.append(cls)
    except Exception:
        pass
    return providers


# KiloCode's own catalogue is 394 ids and it is a paid storefront: 353 of them
# answer `401 PAID_MODEL_AUTH_REQUIRED` to a keyless request, one states a daily
# limit that only money lifts, and one is a classifier that never repeats a
# prompt back. Advertising that list would put hundreds of dead names in the
# picker, so what BeeCode pins is exactly the ids below -- each one answered a
# real request of ours, named this provider explicitly, with `api_key=None`
# forced in the call, and read back a code word buried in a 2048-token filler.
# Measured 2026-09-24 against g4f 8.5.7 (artifacts: C:\tmp\g4add\*.json). Three
# columns per id: "tiny" is the seconds a two-word prompt took, "needle" the
# seconds of the same request wrapped in a 2048-token filler whose buried code
# word came back, and "window" the largest prompt it repeated the code word from
# (`beeagent.core.windows.probe`, ceiling 131072 — 131072 there is where the
# climb stopped, not a refusal, and 32768/65536 are the numbers the endpoint
# itself wrote when it refused).
#
# Four ids needed a second pass after a throttle — glm-5.2 (429), laguna-xs
# (429), nemotron-3-nano-omni (upstream 502) and laguna-s (an empty completion
# at 2048 tokens that answered 7.5 s later) — and are listed at the time they
# finally took.
#
# The mechanism is deliberately not "read the provider's own list": g4f fills
# KiloCode.models by an HTTP GET of that 394-id storefront, so a pin here is the
# only way `/models` stays offline while a name nobody measured cannot appear.
# If g4f drops or un-marks the class, `_keyless_providers()` stops returning it
# and these ids disappear from the catalogue with it — the honest result, rather
# than a menu of names that now reach nothing.
KILOCODE_MEASURED = (
    "kilo-auto/free",                             # 7.1s · 2.9s · 131072
    "kilo-auto/small",                            # 1.4s · 28.0s · 131072
    "nvidia/nemotron-3-super-120b-a12b:free",     # 1.0s · 1.6s · 131072
    "cohere/north-mini-code:free",                # 0.6s · 1.8s · 24576
    "z-ai/glm-5.2:free",                          # 1.4s · 3.1s · 32768
    "poolside/laguna-s-2.1:free",                 # 4.9s · 7.5s · 131072
    "poolside/laguna-xs-2.1:free",                # 1.2s · 4.0s · 131072
    "nex-agi/nex-n2.5-pro:free",                  # 6.3s · 38.3s · 131072
    "nex-agi/nex-n2.5-mini:free",                 # 1.2s · 0.7s · 131072
    "inclusionai/ling-3.0-flash-fin:free",        # 1.2s · 1.7s · 131072
    "inclusionai/ling-3.0-flash-sante:free",      # 1.3s · 1.8s · 131072
    "dots-studio/dots-3-note-preview:free",       # 2.2s · 3.0s · 131072
    "liquid/lfm-2.5-2.6b:free",                   # 1.0s · 1.3s · 65536
    "stepfun/step-3.7-flash:free",                # 2.3s · 3.0s · 131072
    "nvidia/nemotron-3-ultra-550b-a55b:free",     # 1.1s · 1.6s · 131072
    "nvidia/nemotron-3.5-lightning:free",         # 5.6s · 14.4s · 131072
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",  # 1.9s · 2.2s · 131072
)

#: provider class name -> the ids we measured on it, used when the provider
#: ships no static list of its own (see `_provider_models`).
PINNED_MEASURED_MODELS = {"KiloCode": KILOCODE_MEASURED}


def _provider_models(cls) -> list[str]:
    """The ids a pinned provider may be asked for, without touching the network.

    LLM7 and CohereForAI carry their lists as class attributes, so the installed
    package is the source and needs no measuring. KiloCode ships `models = []`
    and fills it only by fetching 394 ids over HTTP, most of which it will not
    serve for free, so its entry comes from `PINNED_MEASURED_MODELS`.
    """
    pinned = PINNED_MEASURED_MODELS.get(getattr(cls, "__name__", ""))
    if pinned is not None:
        return [model for model in pinned if isinstance(model, str)]
    return [model for model in (getattr(cls, "models", None) or [])
            if isinstance(model, str)]


def _pinned_models() -> list[str]:
    """The model ids the pinned providers advertise, read off the installed package.

    Offline and instant — the provider classes carry their lists as attributes,
    and the one provider that does not is pinned to what we measured by hand —
    and the only catalogue BeeCode may claim, since a request now goes nowhere
    else. A name outside it is refused before it leaves the machine ("Model x
    not found" from CohereForAI) or answered 400 by LLM7 ("model_unavailable"):
    measured 2026-09-23, LLM7 rejected all 31 curated ids but its own `default`.
    """
    found: list[str] = []
    for cls in _keyless_providers():
        for model in _provider_models(cls):
            if model not in found:
                found.append(model)
    return found


# g4f's lists overstate what the endpoint behind them answers. Measured
# 2026-09-23 against CohereForAI's Hugging Face space, twice each: `command-r`
# and `command-r-plus` came back with an empty completion and
# `command-r7b-arabic-02-2025` stayed silent for 95 s. Re-measured 2026-09-24
# (g4f 8.5.7): the two empties came back empty again in 7.7 s and 9.0 s, and the
# arabic route was still unanswered at 60 s. They are advertised, so they are
# excluded by name rather than left to be rediscovered as failures.
MEASURED_SILENT = ("command-r", "command-r-plus", "command-r7b-arabic-02-2025")


# What a keyless route means for the user when it says "not now". Measured
# 2026-09-24: of KiloCode's 20 candidate ids, 5 did not answer the first two-word
# prompt (`429 Provider returned error` on glm-5.2, qwen3.8-27b, laguna-xs and
# inkling-small, an upstream `502 ResourceExhausted` on nemotron-3-nano-omni) and
# answered in 1-2 s once the minute turned over. Two never did:
# `qwen/qwen3.8-27b:free` said 429 to eleven of thirteen requests and answered
# the other two only after refusing the long prompt with a token count twelve
# times the one we sent, and `thinkingmachines/inkling-small:free` says "Daily
# limit reached ... Credit" — money, not a minute. Neither is listed.
#
# A throttle is not a dead model, and it must not be retried in a loop here: the
# request that failed already cost the user a turn, and hammering is how a free
# endpoint gets switched off for the whole address. The pool states the same
# thing for its 429 (`providers/pool.py`, `_MESSAGE_FOR`), so this is the
# client-side twin of that hint — wait a minute, or pick another id, once.
def _rate_limited(text: str) -> bool:
    lowered = (text or "").lower()
    return ("429" in lowered or "rate limit" in lowered or "limit reached" in lowered
            or "too many" in lowered)


def _retry_hint(model: str, last_error: str) -> str:
    """The instruction that belongs on a throttled answer: wait, or pick again."""
    if not _rate_limited(last_error):
        return ""
    return (f" — '{model}' is on a free tier that limits requests per minute. "
            "Wait about a minute and ask again, or pick another id with /models; "
            "BeeCode will not retry this in a loop.")


def _providers_for(model: str) -> list:
    """The pinned providers, with whoever advertises `model` moved to the front.

    Nothing is dropped: every pin is still tried in turn, so a provider that
    answers a name it never advertised keeps working and no request is ever
    sent without a provider. Only the order changes, because asking LLM7 for a
    KiloCode id costs a `400 model_unavailable` round trip before the route
    that actually has it, and a throttled minute is worth the saved second.
    """
    ordered = _keyless_providers()
    serving = set(G4fProvider.upstream_map().get(model) or ())
    return ([cls for cls in ordered if getattr(cls, "__name__", "") in serving]
            + [cls for cls in ordered if getattr(cls, "__name__", "") not in serving])


def _reasoning_text(delta) -> str:
    """Extract thinking tokens from a chunk delta, whatever the provider calls them."""
    for attr in ("reasoning", "reasoning_content", "reasoning_content_text", "thinking"):
        val = getattr(delta, attr, None)
        if isinstance(val, str) and val:
            return val
    return ""


async def _await_or_keep(response):
    """Await g4f's reply, unless it already handed back the stream itself.

    `AsyncClient.create(stream=True)` returns a coroutine for some providers and
    an async generator for others — auto-routing gives the generator. Awaiting
    that raised "object async_generator can't be used in 'await' expression",
    which failed every streamed answer and left the agent silently re-asking
    without tokens.
    """
    return await response if inspect.isawaitable(response) else response


class G4fProvider(BaseProvider):
    name = "g4f"
    # Every id here answered through the pinned path in one measured request
    # (2026-09-23, g4f 8.5.1, "Reply with exactly: BEE-OK", retried once). What
    # the old list carried — gemini, claude, gpt-4.x, kimi, qwen, llama,
    # deepseek, glm, grok, sonar — was served by g4f's auto-routing, and that
    # fallback is gone: asked today each of those names dies in g4f's model
    # lookup ("Model gemini-2.5-pro not found") or comes back
    # `400 model_unavailable` from llm7.io, which refuses every id but its own.
    #
    # Widest measured context first: `command-a-03-2025` is the route measured
    # holding a 32k prompt and the config default, the next three answer on the
    # same keyless Cohere space, and `default` is LLM7's only id — it will not
    # say which model is behind it, which is why it comes last.
    #
    # This is the short list `/model` offers first, not the whole menu: the
    # seventeen KiloCode ids measured on 2026-09-24 join it in
    # `discover_models()`, which is what `/models`, the picker and completion
    # read. Nothing is added there that has not answered a request of ours.
    models = [
        "command-a-03-2025",
        "command-r-plus-08-2024",
        "command-r-08-2024",
        "command-r7b-12-2024",
        "default",
    ]

    # Cache for the catalog of the pinned providers.
    _discovered: list[str] | None = None

    @classmethod
    def discover_models(cls) -> list[str]:
        """Every model a pinned provider advertises and actually answers.

        There is nothing else to discover: a request goes to LLM7, CohereForAI
        or KiloCode, and nowhere else, so scanning all of
        `g4f.Provider.__providers__` would list hundreds of names this provider
        cannot serve. Still offline and instant — two classes carry their lists
        as attributes and the third is pinned to the ids we measured — and still
        excludes `MEASURED_SILENT`, the ids they advertise but answer with an
        empty completion or with silence.
        """
        if cls._discovered is not None:
            return list(cls._discovered)
        found: list[str] = []
        seen = set(cls.models)
        found.extend(cls.models)
        for model in _pinned_models():
            if model not in seen and model not in MEASURED_SILENT:
                seen.add(model)
                found.append(model)
        cls._discovered = found
        return list(found)

    _upstream_map: dict[str, list[str]] | None = None

    # A model is only worth recommending if it can hold a real working session:
    # the tool catalog, skills and file dumps spend tokens quickly.
    RECOMMENDED_MIN_TOKENS = 32768

    @classmethod
    def by_window(cls, models: list[str]) -> list[str]:
        """Order models by context window, biggest first.

        A window BeeCode measured itself leads a window a model id merely
        claims — but only while it is wide: measuring `gpt-4` at 2k says it is
        small, so it sinks to where 2k belongs instead of taking third place in
        a list about capacity. `/models` marks ✔ measured and ~ claimed.

        Each window is looked up once: sorting 600 names asked the same question
        600 times and showed it as a visible pause.
        """
        from beeagent.core import windows
        from beeagent.core.context import advertised_window

        sizes = {model: advertised_window(model) for model in set(models)}

        def rank(model: str):
            size = sizes[model]
            proven_wide = bool(windows.measured(model)) and size >= cls.RECOMMENDED_MIN_TOKENS
            return (0 if proven_wide else 1, -size, model)

        return sorted(models, key=rank)

    @classmethod
    def recommended_models(cls, limit: int = 40, models: list[str] = None) -> list[str]:
        """Catalog models that can carry a long session, largest window first.

        `models` accepts a list already ordered by `by_window`, so a caller that
        sorted the catalog for display does not sort it a second time.
        """
        from beeagent.core.context import advertised_window

        pool = cls.discover_models() if models is None else models
        wide = [m for m in pool if advertised_window(m) >= cls.RECOMMENDED_MIN_TOKENS]
        return cls.by_window(wide)[:limit]

    @classmethod
    def upstream_map(cls) -> dict[str, list[str]]:
        """model -> the pinned provider that advertises it (offline, cached).

        The `/models` table prints this under "served by", so scanning all of
        g4f here credited BlackboxPro, Cloudflare and the rest with serving
        models this provider can never ask them for. Only the pinned classes
        answer, so only they are named — and for KiloCode, which ships no list
        of its own, the ids are the ones measured on it.
        """
        if cls._upstream_map is not None:
            return cls._upstream_map
        mapping: dict[str, list[str]] = {}
        for pr in _keyless_providers():
            for model in _provider_models(pr):
                mapping.setdefault(model, []).append(pr.__name__)
        cls._upstream_map = mapping
        return mapping

    @classmethod
    def upstreams(cls) -> list[str]:
        """Provider names usable as a /models filter, most models first."""
        counts: dict[str, int] = {}
        for providers in cls.upstream_map().values():
            for name in providers:
                counts[name] = counts.get(name, 0) + 1
        return [name for name, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    @staticmethod
    def _sanitize_messages(messages: list[dict]) -> list[dict]:
        """Make history provider-safe.

        Free g4f backends reject non-standard history: role "tool" without
        tool_call_id, and custom tool_calls fields. We rewrite:
        - assistant messages: keep only plain text content
        - tool messages: fold into user messages ("[tool result] ...")
        - merge consecutive user messages to keep strict alternation
        """
        sanitized: list[dict] = []
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content") or ""

            if role == "system":
                sanitized.append({"role": "system", "content": content})
                continue

            if role == "assistant":
                # Drop custom tool_calls field; keep plain text only.
                text = content.strip()
                if text:
                    sanitized.append({"role": "assistant", "content": text})
                continue

            if role == "tool":
                text = f"[tool result]\n{content}".strip()
                if sanitized and sanitized[-1]["role"] == "user" and "tool result]" in sanitized[-1]["content"]:
                    sanitized[-1]["content"] += "\n\n" + text
                else:
                    sanitized.append({"role": "user", "content": text})
                continue

            # user (default)
            text = content.strip()
            if text:
                if sanitized and sanitized[-1]["role"] == "user":
                    sanitized[-1]["content"] += "\n\n" + text
                else:
                    sanitized.append({"role": "user", "content": text})

        return sanitized

    async def chat(self, messages: list[dict], model: str = "gpt-4", stream: bool = False) -> str:
        from g4f.client import AsyncClient

        messages = self._sanitize_messages(messages)
        provider_classes = _providers_for(model)
        # Which provider said what. Overwriting one `last_error` in the loop made
        # the message describe whoever was tried last: with KiloCode in the list
        # that turned a quiet Hugging Face space into "You need to sign in to use
        # this model" on the face of a product whose whole promise is no sign-in.
        failed: list[str] = []

        for cls in provider_classes:
            name = getattr(cls, "__name__", "auto")
            client = AsyncClient(provider=cls)
            try:
                response = await _await_or_keep(client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=stream,
                ))
                if stream:
                    parts = []
                    async for chunk in response:
                        if not chunk.choices:
                            continue
                        content = getattr(chunk.choices[0].delta, "content", None)
                        if content:
                            parts.append(content)
                    text = "".join(parts)
                else:
                    message = response.choices[0].message
                    text = getattr(message, "content", None) or ""
                    if not text.strip() and _reasoning_text(message):
                        # A reasoning-only reply means the model never answered;
                        # treat it as a failure so the next provider is tried.
                        raise ValueError("model returned reasoning only")
                if text and text.strip():
                    return text
                failed.append(f"{name}: empty response")
            except Exception as e:
                failed.append(f"{name}: {' '.join(str(e).split())[:160]}")

        last_error = "; ".join(failed) or "no provider answered"
        raise RuntimeError(f"g4f failed on all providers: {last_error}"
                           f"{_retry_hint(model, last_error)}")

    async def chat_stream(self, messages: list[dict], model: str = "gpt-4") -> AsyncIterator:
        from g4f.client import AsyncClient

        messages = self._sanitize_messages(messages)
        provider_classes = _providers_for(model)
        last_error = None

        for cls in provider_classes:
            client = AsyncClient(provider=cls)
            # Bound before the request: a provider that refuses at create() —
            # which is what a KiloCode 429 does — used to die here on
            # `UnboundLocalError: got_any`, and the user read that instead of
            # the rate limit that actually happened.
            got_any = False
            try:
                stream = await _await_or_keep(client.chat.completions.create(
                    model=model,
                    messages=messages,
                    stream=True,
                ))
                got_any = False
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    content = getattr(delta, "content", None)
                    if content:
                        got_any = True
                        yield ("content", content)
                    reasoning = _reasoning_text(delta)
                    if reasoning:
                        yield ("reasoning", reasoning)
                if got_any:
                    return
                last_error = "empty stream"
                # empty stream -> try next provider
            except Exception as e:
                if got_any:
                    # Half an answer already reached the user. Falling through to
                    # the next provider here glued two different replies into one
                    # message — the caller resets the stream and asks again, which
                    # is the only honest recovery left.
                    raise
                # Swallowing this left the user staring at "empty response" while
                # the real cause (rate limit, dead endpoint) went unsaid.
                last_error = f"{getattr(cls, '__name__', 'auto')}: {e}"

        raise RuntimeError(f"g4f streamed nothing for '{model}': {last_error}"
                           f"{_retry_hint(model, str(last_error))}")
