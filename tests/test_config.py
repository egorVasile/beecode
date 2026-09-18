import json
import tempfile
from pathlib import Path
from pydantic import ValidationError
import pytest

from beeagent.config.schema import BeeConfig, EconomyConfig, CustomProvider
from beeagent.config.loader import load_config, save_config

def test_default_config():
    config = BeeConfig()
    assert config.model == "gpt-4"
    assert config.provider == "g4f"
    assert config.mode == "normal"
    assert config.max_turns == 50

def test_economy_config_defaults():
    econ = EconomyConfig()
    assert econ.cache_enabled is True
    assert econ.batch_tools is True
    assert econ.smart_routing is True

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
        assert loaded.model == "gpt-4"
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
