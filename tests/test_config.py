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


def test_a_torn_config_starts_on_defaults_instead_of_crashing(tmp_path, capsys):
    """A half-written beeagent.json used to make the agent unstartable."""
    (tmp_path / "beeagent.json").write_text('{"model": "gpt-4", "max_turns": ', encoding="utf-8")

    loaded = load_config(str(tmp_path))

    assert loaded.model != "gpt-4", "the broken file is not applied"
    assert loaded.max_turns == BeeConfig().max_turns
    assert "beeagent.json" in capsys.readouterr().out, "the user is told which file failed"


def test_an_invalid_value_falls_back_and_says_so(tmp_path, capsys):
    (tmp_path / "beeagent.json").write_text('{"max_turns": "many"}', encoding="utf-8")

    loaded = load_config(str(tmp_path))

    assert loaded.max_turns == BeeConfig().max_turns
    assert "beeagent.json" in capsys.readouterr().out


def test_unknown_keys_are_announced_before_they_are_dropped(tmp_path, capsys):
    """pydantic ignores extras and the next save erases them from disk."""
    (tmp_path / "beeagent.json").write_text('{"tempreture": 0.2}', encoding="utf-8")

    load_config(str(tmp_path))

    out = capsys.readouterr().out
    assert "tempreture" in out


def test_save_leaves_no_temp_file_and_round_trips(tmp_path):
    config = BeeConfig(model="command-a-03-2025", max_turns=7)
    save_config(config, str(tmp_path))

    assert list(tmp_path.glob("*.tmp")) == []
    assert load_config(str(tmp_path)).max_turns == 7
