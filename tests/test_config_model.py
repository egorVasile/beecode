"""The config model is the first thing BeeCode touches, and it owns one promise:
a value it cannot read is refused, loudly, naming the line to fix.
"""
import pytest

from beeagent.config.model import Model, ValidationError, default
from beeagent.config.schema import BeeConfig, CustomProvider


class Inner(Model):
    label: str = "x"


class Outer(Model):
    count: int = 1
    flag: bool = False
    name: str
    tags: list[str] = default(list)
    inner: Inner = default(Inner)
    mapping: dict[str, int] = default(dict)


def test_a_value_that_cannot_be_read_says_which_one():
    with pytest.raises(ValidationError) as raised:
        Outer(name="ok", count="many")
    assert "count" in str(raised.value)
    assert "many" in str(raised.value)


def test_every_broken_field_is_listed_not_just_the_first():
    with pytest.raises(ValidationError) as raised:
        Outer(name=5, count="many", flag="yes")
    problems = raised.value.problems
    assert len(problems) == 3, problems


def test_a_missing_required_field_is_a_refusal_not_a_silent_default():
    with pytest.raises(ValidationError) as raised:
        Outer(count=2)
    assert "name: required" in str(raised.value)


def test_an_entry_of_a_nested_model_names_the_entry_it_is_in():
    """`custom_providers[1].url` is the line a person can find in beeagent.json;
    `url: expected text` alone would send them looking through the whole file."""
    config = BeeConfig(custom_providers=[
        CustomProvider(name="a", type="ollama", url="http://x", model="m")])
    with pytest.raises(ValidationError) as raised:
        BeeConfig(custom_providers=[
            {"name": "a", "type": "ollama", "url": "http://x", "model": "m"},
            {"name": "b", "type": "ollama", "url": 11434, "model": "m"}])
    assert "custom_providers[1].url" in str(raised.value)
    assert config.custom_providers[0].url == "http://x"


def test_a_hand_written_number_still_counts_as_one():
    """The file is edited by hand. `"max_turns": "10"` is a person's intent, not a
    type error, and the alternative is the whole config falling back to defaults."""
    assert BeeConfig(max_turns="10").max_turns == 10
    assert Outer(name="n", count="7").count == 7
    with pytest.raises(ValidationError):
        Outer(name="n", count="")


def test_two_configs_do_not_share_one_list():
    first = BeeConfig()
    first.permissions.allowed.append("bash")
    assert BeeConfig().permissions.allowed == []
    assert Outer(name="n").tags == []


def test_a_key_from_a_newer_beecode_is_ignored_and_dumps_back_what_it_knows():
    outer = Outer(name="n", tempreture=0.2)
    assert "tempreture" not in outer.model_dump()
    assert outer.model_dump()["inner"] == {"label": "x"}


def test_a_config_equal_to_another_is_the_same_config():
    assert BeeConfig() == BeeConfig()
    assert BeeConfig(model="gpt-4") != BeeConfig()
