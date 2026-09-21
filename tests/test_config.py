import json
import tempfile
from pathlib import Path
from pydantic import ValidationError
import pytest

from beeagent.config.schema import BeeConfig, EconomyConfig, CustomProvider
from beeagent.config.loader import load_config, save_config

def test_default_config():
    config = BeeConfig()
    assert config.model == "command-a-03-2025"
    assert config.provider == "g4f"
    assert config.mode == "normal"
    assert config.max_turns == 50

def test_economy_config_defaults():
    econ = EconomyConfig()
    assert econ.cache_enabled is True
    assert econ.cache_ttl_minutes > 0
    # batch_tools / smart_routing were advertised but no code read them; the
    # cache is the whole of economy mode.
    assert not hasattr(econ, "batch_tools")
    assert not hasattr(econ, "smart_routing")

def test_permissions_default_asks():
    """Writing to disk must not happen on a model's say-so."""
    from beeagent.config.schema import PermissionsConfig

    config = BeeConfig()
    assert config.permissions.mode == "ask"
    assert config.permissions.allowed == []
    assert PermissionsConfig(mode="nonsense").mode == "nonsense"

def test_context_window_is_a_ceiling_not_a_pin():
    assert BeeConfig().max_context_tokens == 0        # 0 = detect from the model

def test_custom_provider():
    prov = CustomProvider(
        name="my-ollama",
        type="ollama",
        url="http://localhost:11434",
        model="codellama"
    )
    assert prov.name == "my-ollama"
    assert prov.type == "ollama"

def test_loader_round_trip():
    with tempfile.TemporaryDirectory() as tmp:
        config = BeeConfig(model="gpt-4", provider="openai", max_turns=10)
        save_config(config, workdir=tmp)
        loaded = load_config(workdir=tmp)
        assert loaded.model == "gpt-4"
        assert loaded.provider == "openai"
        assert loaded.max_turns == 10

def test_loader_missing_file():
    with tempfile.TemporaryDirectory() as tmp:
        loaded = load_config(workdir=tmp)
        assert loaded.model == "command-a-03-2025"
        assert loaded.max_turns == 50
        assert loaded.economy.cache_enabled is True
        assert loaded.custom_providers == []

def test_validation_error_invalid_type():
    with pytest.raises(ValidationError):
        BeeConfig(max_turns="abc")

def test_economy_nested_default():
    config = BeeConfig()
    assert config.economy.cache_enabled is True

def test_custom_providers_empty_list():
    config = BeeConfig()
    assert config.custom_providers == []
