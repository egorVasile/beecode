from beeagent.config.schema import BeeConfig, EconomyConfig, CustomProvider

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
