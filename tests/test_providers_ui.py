"""Provider management: free-tier presets, keys, provider-scoped model lists."""
import json

import pytest

from beeagent.config.loader import load_config, save_config
from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.providers.presets import BY_NAME, ENDPOINTS, key_for
from beeagent.ui.commands import ReplContext, available_models, dispatch


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ctx(config=None, agent=None):
    return ReplContext(agent=agent, config=config or BeeConfig(), session=Session())


def _text(result):
    from rich.console import Console
    console = Console(width=200)
    with console.capture() as cap:
        console.print(result.output)
    return cap.get()


# --- presets ---------------------------------------------------------------

def test_presets_are_wellformed():
    names = [e.name for e in ENDPOINTS]
    assert len(names) == len(set(names))
    for endpoint in ENDPOINTS:
        assert endpoint.url.startswith("https://"), endpoint.name
        assert endpoint.env.isupper(), endpoint.name
        assert endpoint.signup.startswith("https://"), endpoint.name
        assert endpoint.free, endpoint.name


def test_key_lookup_prefers_config_then_env(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "from-env")
    assert key_for(BY_NAME["groq"], {}) == "from-env"
    assert key_for(BY_NAME["groq"], {"groq": "from-config"}) == "from-config"
    monkeypatch.delenv("GROQ_API_KEY")
    assert key_for(BY_NAME["groq"], {}) == ""


def test_agent_registers_only_providers_with_keys():
    from beeagent.core.agent import Agent

    plain = Agent(config=BeeConfig())
    assert "groq" not in plain.providers.list_names()
    assert plain.ready_presets == []

    keyed = Agent(config=BeeConfig(api_keys={"groq": "gsk_test"}))
    assert "groq" in keyed.providers.list_names()
    assert keyed.ready_presets == ["groq"]


def test_api_keys_survive_a_config_round_trip():
    save_config(BeeConfig(api_keys={"groq": "gsk_x"}), ".")
    assert load_config(".").api_keys == {"groq": "gsk_x"}


# --- commands --------------------------------------------------------------

def test_providers_command_shows_key_state():
    """The table and the picker have to agree: two rows, both keyless.

    It used to list every endpoint with "no key, run /key ..." next to it, which
    is a different promise from the one on the box.
    """
    out = _text(dispatch(_ctx(), "/providers"))
    assert "g4f" in out and "pool" in out
    assert "no key" not in out, "an endpoint nobody can use must not be offered"
    assert "groq" not in out and "openrouter" not in out


def test_provider_without_key_is_refused_with_instructions():
    result = dispatch(_ctx(), "/provider groq")
    assert "key" in result.output.plain.lower()
    assert "https://" in result.output.plain     # where to get one


def test_key_command_stores_registers_and_never_echoes():
    from beeagent.core.agent import Agent

    config = BeeConfig()
    agent = Agent(config=config)
    ctx = _ctx(config=config, agent=agent)
    result = dispatch(ctx, "/key groq gsk_supersecret_1234")
    assert "gsk_supersecret_1234" not in result.output.plain
    assert "1234" in result.output.plain          # only the tail is shown
    assert config.api_keys["groq"] == "gsk_supersecret_1234"
    assert "groq" in agent.providers.list_names()
    # and it landed on disk
    assert json.loads(open("beeagent.json", encoding="utf-8").read())["api_keys"]["groq"]

    removed = dispatch(ctx, "/key groq remove")
    assert "removed" in removed.output.plain
    assert "groq" not in config.api_keys


def test_provider_switch_sets_a_model_from_that_provider():
    from beeagent.core.agent import Agent

    config = BeeConfig(api_keys={"groq": "gsk_x"})
    ctx = _ctx(config=config, agent=Agent(config=config))
    dispatch(ctx, "/provider groq")
    assert config.provider == "groq"
    assert config.model in BY_NAME["groq"].models


def test_models_are_scoped_to_the_selected_provider(monkeypatch):
    from beeagent.core.agent import Agent

    config = BeeConfig(provider="groq", api_keys={"groq": "gsk_x"})
    agent = Agent(config=config)
    provider = agent.providers.get("groq")

    async def boom():
        raise AssertionError("completion must not hit the network")

    monkeypatch.setattr(provider, "list_models", boom)
    models = available_models(_ctx(config=config, agent=agent))   # fetch=False
    assert models == list(BY_NAME["groq"].models)                 # static fallback

    async def live():
        return ["llama-3.3-70b", "qwen3-32b"]

    monkeypatch.setattr(provider, "list_models", live)
    fetched = available_models(_ctx(config=config, agent=agent), fetch=True)
    assert fetched == ["llama-3.3-70b", "qwen3-32b"]
    # the cache now lets completion answer without the network again
    monkeypatch.setattr(provider, "list_models", boom)
    assert available_models(_ctx(config=config, agent=agent)) == fetched


def test_g4f_models_show_upstream_and_filter():
    from beeagent.core.agent import Agent

    config = BeeConfig(provider="g4f")
    ctx = _ctx(config=config, agent=Agent(config=config))
    out = _text(dispatch(ctx, "/models"))
    assert "served by" in out                    # which g4f provider answers
    filtered = _text(dispatch(ctx, "/models LLM7"))
    assert "served by" in filtered
    assert "default" in filtered                 # the id LLM7 actually answers
    empty = dispatch(ctx, "/models zzz-no-such-model")
    assert "nothing matches" in _text(empty).lower()


def test_a_catalogue_behind_a_request_is_read_from_the_cache_like_every_other():
    """crax lists its models over HTTP, on an endpoint with a per-minute limit per
    address. Completion runs on every keystroke, so a keystroke must not spend one."""
    from beeagent.core.agent import Agent

    config = BeeConfig(provider="crax", api_keys={"crax": "crk_live_test"})
    agent = Agent(config=config)
    provider = agent.providers.get("crax")
    ctx = _ctx(config=config, agent=agent)

    def refusing():
        raise AssertionError("completion must not hit the network")

    def discover(fn):
        # An instance attribute, so the class is untouched for the next test.
        provider.__dict__["discover_models"] = fn

    discover(refusing)
    assert available_models(ctx) == list(provider.models), "the static list answers"

    discover(lambda: ["qwen3.8-max", "gpt-5-6-luna"])
    assert available_models(ctx, fetch=True) == ["qwen3.8-max", "gpt-5-6-luna"]

    discover(refusing)
    assert available_models(ctx) == ["qwen3.8-max", "gpt-5-6-luna"], "the cache answers now"
