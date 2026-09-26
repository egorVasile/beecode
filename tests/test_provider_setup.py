"""The shared endpoint door: validation, the four fields, the masked save.

Everything `/providers add`, `/providers edit`, `/providers key` and `/key`
write goes through `core/provider_setup.py`; these tests are the contract both
UIs inherit. Nothing here reaches the network — discovery is tested only for
the sentences it must produce before it ever calls out.
"""
import json

import pytest

from beeagent.config.loader import load_config
from beeagent.config.schema import BeeConfig, CustomProvider
from beeagent.core import provider_setup as ps


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- the pool as text -------------------------------------------------------

def test_keys_of_splits_commas_keeps_order_and_drops_blanks():
    assert ps.keys_of("a, b ,,a") == ["a", "b"]
    assert ps.keys_of(["x", "y", "x"]) == ["x", "y"]
    assert ps.keys_of(None) == []


def test_no_key_spellings_never_become_a_key_called_dash():
    assert ps.keys_text("-,none,nokey,real-key") == "real-key"
    assert ps.keys_text("-") == ""


# --- validation -------------------------------------------------------------

def test_url_must_be_an_http_shape():
    assert ps.url_refusal("") != ""
    assert ps.url_refusal("api.example.com/v1") != ""
    assert ps.url_refusal("file:///etc/passwd") != ""
    assert ps.url_refusal("https://api.example.com/v1") == ""


def test_name_shape_and_reserved_words():
    assert ps.name_refusal("My Bot") != ""
    assert ps.name_refusal("groq-2.api_v1") == ""
    for word in ("add", "key", "models", "remove", "use", "edit"):
        assert ps.name_refusal(word) != "", word


def test_refusals_never_carry_a_whole_key():
    leak = ps.url_refusal("gsksecretvalue12345678")
    assert "gsksecretvalue12345678" not in leak


# --- models text -------------------------------------------------------------

def test_split_models_keeps_spaces_and_deduplicates():
    assert ps.split_models("a, b ,a") == ["a", "b"]
    assert ps.split_models(["Claude 3.5 Sonnet", "a"]) == ["Claude 3.5 Sonnet", "a"]


# --- masking ------------------------------------------------------------------

def test_mask_shows_only_the_last_four():
    assert ps.mask_key("sk-abcdef1234") == "…1234"
    assert "abcdef" not in ps.mask_key("sk-abcdef1234")
    short = ps.mask_key("abc")
    assert "abc" not in short, "a four-character tail is the whole key"
    assert ps.key_tails([]) in ("none", "нет")


# --- current() prefills the form ----------------------------------------------

def test_current_prefills_custom_endpoint_and_keys():
    config = BeeConfig(custom_providers=[CustomProvider(
        name="mine", type="openai_compat", url="https://api.mine.test/v1",
        model="m0", key="k-one9999,k-two8888")])
    fields = ps.current(config, "mine")
    assert fields.url == "https://api.mine.test/v1"
    assert fields.key_list == ["k-one9999", "k-two8888"]
    assert fields.models == ["m0"]


def test_current_prefills_a_preset_from_its_own_facts():
    fields = ps.current(BeeConfig(), "groq")
    assert fields.url == "https://api.groq.com/openai/v1"
    assert "llama-3.3-70b-versatile" in fields.models


# --- apply(): the one write both doors share -----------------------------------

def _new(name="mine", url="https://api.mine.test/v1", keys="sk-one9999,sk-two8888",
         models=("model one", "m2")):
    return ps.Fields(name=name, url=url, keys=keys, models=list(models))


def test_apply_saves_endpoint_pool_and_models_and_the_file_round_trips():
    config = BeeConfig()
    message, refusal = ps.apply(config, None, _new())
    assert refusal == "", refusal
    entry = config.custom_providers[0]
    assert entry.name == "mine" and entry.url == "https://api.mine.test/v1"
    assert entry.key == "sk-one9999,sk-two8888", "the pool is stored in order"
    assert entry.model == "model one"
    assert ps.cached_models("mine") == ["model one", "m2"], "the list survives one `model`"
    # and it lands on disk in the config the user already owns
    back = load_config(".")
    assert back.custom_providers[0].key == "sk-one9999,sk-two8888"
    # the message names keys only by their tails
    assert "sk-one9999" not in message and "…8888" in message


def test_apply_refuses_an_endpoint_with_nothing_to_say():
    message, refusal = ps.apply(BeeConfig(), None,
                                ps.Fields(name="empty", url="https://x.test/v1"))
    assert message == "" and "nothing to save" in refusal


def test_apply_refuses_bad_shapes_without_touching_the_config():
    config = BeeConfig()
    _msg, refusal = ps.apply(config, None, ps.Fields(name="My Bot",
                                                     url="https://x.test/v1", keys="k"))
    assert refusal and not config.custom_providers
    _msg, refusal = ps.apply(config, None, ps.Fields(name="ok", url="not-a-url",
                                                     keys="k"))
    assert refusal and not config.custom_providers


def test_apply_refuses_shadowing_a_built_in_address():
    config = BeeConfig()
    _msg, refusal = ps.apply(config, None, ps.Fields(
        name="groq", url="https://somewhere.else/v1", keys="gsk_x1234"))
    assert "built-in" in refusal and not config.custom_providers


def test_apply_on_a_preset_stores_only_the_key_pool():
    config = BeeConfig()
    message, refusal = ps.apply(config, None, ps.Fields(
        name="groq", url="", keys="gsk_one1111,gsk_two2222"))
    assert refusal == "", refusal
    assert config.api_keys["groq"] == "gsk_one1111,gsk_two2222"
    assert config.custom_providers == [], "groq keeps its shipped address and class"
    assert "…2222" in message and "gsk_one1111" not in message


def test_apply_editing_a_preset_keeps_the_stored_url_and_says_active_door():
    config = BeeConfig()
    message, refusal = ps.apply(config, None, ps.Fields(
        name="groq", url="https://api.groq.com/openai/v1", keys="gsk_k9999",
        models=["llama-3.3-70b-versatile"]))
    assert refusal == ""
    assert "…9999" in message and "gsk_k9999" not in message
    assert ps.cached_models("groq") == ["llama-3.3-70b-versatile"]


def test_rename_carries_the_keys_and_replaces_the_entry():
    config = BeeConfig(custom_providers=[CustomProvider(
        name="old", type="openai_compat", url="https://api.old.test/v1",
        model="m", key="k-keep0000")])
    config.api_keys["old"] = "k-keep0000"
    fields = ps.Fields(name="new", url="https://api.old.test/v1",
                       keys="k-keep0000", models=["m"], was="old")
    message, refusal = ps.apply(config, None, fields)
    assert refusal == "", refusal
    assert [c.name for c in config.custom_providers] == ["new"]
    assert config.api_keys.get("new") == "k-keep0000"
    assert "old" not in config.api_keys


def test_apply_refuses_the_names_the_program_itself_owns():
    for built in ("g4f", "pool"):
        _msg, refusal = ps.apply(BeeConfig(), None, ps.Fields(
            name=built, url="https://x.test/v1", keys="k1234567"))
        assert "built into BeeCode" in refusal


# --- discover() pre-flight ------------------------------------------------------

def test_discover_refuses_without_ever_calling_out():
    models, dialect, refusal = ps.discover("telnet://nope", "k")
    assert models == [] and dialect == "" and refusal != ""


# --- forget() --------------------------------------------------------------------

def test_forget_drops_entry_keys_and_reports_what_went():
    config = BeeConfig(custom_providers=[CustomProvider(
        name="gone", type="openai_compat", url="https://g.test/v1",
        model="m", key="k-gone1234")])
    config.api_keys["gone"] = "k-gone1234"
    parts, refusal = ps.forget(config, None, "gone")
    assert refusal == "" and config.custom_providers == [] and "gone" not in config.api_keys
    assert "key removed" in " ".join(parts) and "endpoint removed" in " ".join(parts)


def test_forget_of_a_name_holding_nothing_says_so():
    parts, refusal = ps.forget(BeeConfig(), None, "ghost")
    assert parts == [] and refusal == ""


# --- exists() ---------------------------------------------------------------------

def test_exists_names_every_kind_of_already():
    config = BeeConfig(custom_providers=[CustomProvider(
        name="mine", type="openai_compat", url="https://m.test/v1", model="m")])
    assert ps.exists(config, "mine") and ps.exists(config, "groq") and ps.exists(config, "g4f")
    assert not ps.exists(config, "stranger")


def test_a_cached_model_list_expires_with_the_version_that_wrote_it(tmp_path, monkeypatch):
    """The picker must not outlive the endpoint's catalogue.

    `saved_at` and `version` were written into the cache from the start and read by
    nobody, so a list saved before `beecode --update` kept being offered after it —
    and on 2026-09-26, when crax dropped twelve of its thirteen ids, that was the
    difference between a working install and "the models do not answer".
    """
    import json
    import time

    from beeagent import __version__
    from beeagent.core import provider_setup

    monkeypatch.chdir(tmp_path)
    name = "mine"

    def write(models, version=__version__, saved_at=None):
        path = provider_setup.models_cache_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "saved_at": saved_at if saved_at is not None else time.time(),
            "version": version, "models": models}), encoding="utf-8")

    write(["glm-5.3"])
    assert provider_setup.cached_models(name) == ["glm-5.3"]

    write(["glm-5.3"], version="8.0.0")
    assert provider_setup.cached_models(name) == [], "a list from another build is not a fact"

    write(["glm-5.3"], version="")
    assert provider_setup.cached_models(name) == [], "an unattributed list is not either"

    write(["glm-5.3"], saved_at=time.time() - 15 * 86400)
    assert provider_setup.cached_models(name) == [], "the cache has to expire"
