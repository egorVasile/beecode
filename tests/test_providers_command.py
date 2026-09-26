"""`/providers` as the one endpoint door — driven through `dispatch`, not under it.

A test that calls a helper directly does not prove the command works, so every
case here types the line a user types: `dispatch(ctx, "/providers …")` for the
commands a line answers on its own, `repl.try_guided` for the interviews the
classic REPL must await, with the prompt primitives of `provider_form` scripted
like a keystroke sequence. `/model` and `/provider` are checked to land on the
same handlers as their plurals.
"""
import asyncio
import json

import pytest

from beeagent.config.loader import load_config
from beeagent.config.schema import BeeConfig, CustomProvider
from beeagent.core.session import Session
from beeagent.ui import provider_form
from beeagent.ui.commands import (
    COMMANDS, ReplContext, dispatch, get_suggestions, visible_commands,
)
from beeagent.ui.repl import try_guided, _picker_specs


@pytest.fixture(autouse=True)
def in_tmp(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _ctx(config=None):
    return ReplContext(agent=None, config=config or BeeConfig(), session=Session())


def _mine_config(**kw):
    return BeeConfig(
        custom_providers=[CustomProvider(name="mine", type="openai_compat",
                                         url="https://api.mine.test/v1", model="m0")],
        api_keys={"mine": "k-old0000"}, **kw)


def _text(result):
    from rich.console import Console

    console = Console(width=200)
    with console.capture() as cap:
        console.print(result.output)
    return cap.get()


# --- one door each -----------------------------------------------------------

def test_visible_commands_advertise_the_plurals_only():
    visible = {c.name for c in visible_commands()}
    assert {"models", "providers"} <= visible
    assert "model" not in visible and "provider" not in visible
    # …but they are still commands of the registry, dispatchable and completable
    assert {"model", "provider"} <= {c.name for c in COMMANDS}


def test_hidden_aliases_still_complete_when_typed():
    texts = {s.text for s in get_suggestions("/mod", {"model": ["m"]})}
    assert "/model" in texts and "/models" in texts


def test_help_table_shows_the_two_doors_not_four():
    out = _text(dispatch(_ctx(), "/help"))
    assert "/models" in out and "/providers" in out
    assert "/model <name>" not in out, "the hidden alias row must not be advertised"
    assert "/provider <name>" not in out


def test_model_alias_goes_to_the_models_handler():
    ctx = _ctx()
    result = dispatch(ctx, "/model command-a-03-2025")
    assert "model →" in result.output.plain
    assert ctx.config.model == "command-a-03-2025"
    assert json.loads(open("beeagent.json", encoding="utf-8").read())["model"] \
        == "command-a-03-2025", "the switch survives a restart"


def test_provider_alias_and_plural_answer_identically():
    ctx = _ctx(BeeConfig())
    alias = dispatch(ctx, "/provider crax").output.plain
    plural = dispatch(ctx, "/providers crax").output.plain
    assert alias == plural, "one handler, not two that drift"
    assert ctx.config.provider == "g4f", "the refusal changes nothing"
    assert "key" in alias and "https://" in alias


def test_providers_use_switches():
    ctx = _ctx()
    assert "provider → g4f" in dispatch(ctx, "/providers use g4f").output.plain
    assert dispatch(_ctx(), "/providers use").output.plain.count("name") >= 1


# --- the table ------------------------------------------------------------------

def test_bare_providers_lists_the_two_free_doors():
    out = _text(dispatch(_ctx(), "/providers"))
    assert "g4f" in out and "pool" in out
    assert "groq" not in out, "an endpoint nobody keyed is not offered"
    assert "no key" not in out


def test_installed_endpoint_without_a_key_is_named_not_filed():
    """The complaint's own words: name the keyless endpoint and its door."""
    config = BeeConfig(custom_providers=[CustomProvider(
        name="lonely", type="openai_compat", url="https://lonely.test/v1", model="m")])
    out = _text(dispatch(_ctx(config), "/providers"))
    assert "lonely" in out and "https://lonely.test/v1" in out
    assert "needs a key" in out and "/providers key lonely" in out


def test_keyed_preset_row_shows_only_the_tail():
    config = BeeConfig(api_keys={"groq": "gsk_supersecret1234"})
    out = _text(dispatch(_ctx(config), "/providers"))
    assert "groq" in out and "…1234" in out
    assert "gsk_supersecret1234" not in out


# --- sub-commands on the line ------------------------------------------------------

def test_providers_key_inline_replaces_the_pool_for_one_provider():
    ctx = _ctx(_mine_config())
    result = dispatch(ctx, "/providers key mine sk-live-4321,sk-more-8765")
    plain = result.output.plain
    assert ctx.config.api_keys["mine"] == "sk-live-4321,sk-more-8765"
    assert "sk-live-4321" not in plain and "…4321" in plain and "…8765" in plain
    assert load_config(".").api_keys["mine"] == "sk-live-4321,sk-more-8765"


def test_providers_key_without_a_name_is_refused():
    assert "name the provider" in dispatch(_ctx(), "/providers key").output.plain


def test_providers_models_sets_the_whole_list():
    ctx = _ctx(_mine_config())
    result = dispatch(ctx, "/providers models mine alpha,beta gamma")
    assert "alpha" in result.output.plain
    assert [c.model for c in ctx.config.custom_providers] == ["alpha"]
    from beeagent.core import provider_setup

    assert provider_setup.cached_models("mine") == ["alpha", "beta gamma"], \
        "the spaced id stays one model, the list outlives the single `model` field"


def test_providers_models_without_a_list_asks_and_says_what_was_found(monkeypatch):
    from beeagent.core import provider_setup

    monkeypatch.setattr(provider_setup, "discover",
                        lambda url, keys, timeout=12.0: (["d1", "d2"], "openai", ""))
    ctx = _ctx(_mine_config())
    out = dispatch(ctx, "/providers models mine").output.plain
    assert "discovered 2" in out and "d1" in out
    assert provider_setup.cached_models("mine") == ["d1", "d2"]


def test_providers_models_says_loudly_when_the_endpoint_says_nothing(monkeypatch):
    from beeagent.core import provider_setup

    monkeypatch.setattr(provider_setup, "discover",
                        lambda url, keys, timeout=12.0: ([], "", "answered 401"))
    ctx = _ctx(_mine_config())
    out = dispatch(ctx, "/providers models mine").output.plain
    assert "nothing found" in out and "401" in out
    assert [c.model for c in ctx.config.custom_providers] == ["m0"], "nothing changed"


def test_providers_remove_takes_away_only_yours():
    ctx = _ctx(_mine_config())
    out = dispatch(ctx, "/providers remove mine").output.plain
    assert "mine" in out and "endpoint removed" in out
    assert ctx.config.custom_providers == [] and "mine" not in ctx.config.api_keys


def test_providers_remove_refuses_the_built_in_doors_in_so_many_words():
    out = _text(dispatch(_ctx(), "/providers remove g4f"))
    assert "built into BeeCode" in out
    out = _text(dispatch(_ctx(), "/providers remove groq"))
    assert "built-in endpoint" in out and "/key groq remove" in out
    out = _text(dispatch(_ctx(_mine_config()), "/providers remove ghost"))
    assert "no custom endpoint" in out


# --- the guided interview (classic REPL), scripted like keystrokes ---------------------

def _script(monkeypatch, answers, yes=()):
    """Replace the prompt primitives with a keystroke queue."""
    ask_queue, yes_queue = list(answers), list(yes)
    seen = []

    async def fake_ask(title, default="", password=False):
        seen.append(("ask", title))
        if not ask_queue:
            raise AssertionError(f"the interview asked more than scripted: {title}")
        return ask_queue.pop(0)

    async def fake_yes(title):
        seen.append(("yes", title))
        if not yes_queue:
            raise AssertionError("a replace question nobody scripted")
        return yes_queue.pop(0)

    monkeypatch.setattr(provider_form, "_ask", fake_ask)
    monkeypatch.setattr(provider_form, "_yes_no", fake_yes)
    return seen


def test_try_guided_ignores_lines_that_need_no_interview():
    ctx = _ctx()
    assert asyncio.run(try_guided(ctx, "providers", [])) is None
    assert asyncio.run(try_guided(ctx, "providers", ["use", "g4f"])) is None
    assert asyncio.run(try_guided(ctx, "providers", ["key", "mine", "k1,k2"])) is None
    assert asyncio.run(try_guided(ctx, "models", ["add"])) is None


def test_providers_add_guided_saves_every_field_and_rereads_it(monkeypatch):
    ctx = _ctx()
    _script(monkeypatch, ["newsrv", "https://api.new.test/v1",
                          "sk-secret9999", "",           # one key, blank line finishes
                          "m-one,m2"])
    result = asyncio.run(try_guided(ctx, "providers", ["add"]))
    assert result is not None
    plain = result.output.plain
    assert "newsrv" in plain and "…9999" in plain
    assert "sk-secret9999" not in plain, "the typed key is never echoed whole"

    back = load_config(".")
    entry = back.custom_providers[0]
    assert entry.name == "newsrv" and entry.url == "https://api.new.test/v1"
    assert entry.key == "sk-secret9999" and entry.model == "m-one"
    assert back.api_keys["newsrv"] == "sk-secret9999"
    from beeagent.core import provider_setup

    assert provider_setup.cached_models("newsrv") == ["m-one", "m2"]
    # and the command itself still answers over the saved endpoint
    assert "newsrv" in _text(dispatch(_ctx(back), "/providers"))


def test_providers_add_on_an_existing_name_is_an_edit_said_out_loud(monkeypatch):
    ctx = _ctx(_mine_config())
    # name collides → prefill; Enter keeps the stored url; keep (not replace) the
    # stored key and add a second one; keep the models.
    _script(monkeypatch, ["mine", "", "k-new1111", "", "m0"], yes=[False])
    result = asyncio.run(try_guided(ctx, "providers", ["add"]))
    plain = result.output.plain
    assert "already exists" in plain and "edits" in plain
    assert len(ctx.config.custom_providers) == 1, "no duplicate door"
    assert ctx.config.api_keys["mine"] == "k-old0000,k-new1111"
    assert "…0000" in plain and "…1111" in plain
    assert "k-old0000," not in plain.replace("…0000", "")  # only tails on screen


def test_providers_edit_guided_replaces_only_the_key_of_a_preset(monkeypatch):
    ctx = _ctx(BeeConfig(api_keys={"groq": "gsk_old0000"}))
    # the preset's address is not asked (it is shipped); replace the pool with
    # one new key, blank line ends it, then keep the model list.
    _script(monkeypatch, ["gsk_new7777", "", "llama-3.3-70b-versatile"], yes=[True])
    result = asyncio.run(try_guided(ctx, "providers", ["edit", "groq"]))
    assert "groq" in result.output.plain and "…7777" in result.output.plain
    assert "gsk_old0000" not in result.output.plain
    assert ctx.config.api_keys["groq"] == "gsk_new7777"
    assert ctx.config.custom_providers == [], "groq keeps its own class and URL"


def test_providers_key_guided_pools_into_one_provider(monkeypatch):
    ctx = _ctx(_mine_config())
    _script(monkeypatch, ["k-second2222", ""], yes=[False])
    result = asyncio.run(try_guided(ctx, "providers", ["key", "mine"]))
    assert ctx.config.api_keys["mine"] == "k-old0000,k-second2222"
    assert "…0000" in result.output.plain and "…2222" in result.output.plain


def test_providers_add_cancelled_changes_nothing(monkeypatch):
    ctx = _ctx()

    async def abort(title, default="", password=False):
        raise provider_form._Cancelled()

    monkeypatch.setattr(provider_form, "_ask", abort)
    result = asyncio.run(try_guided(ctx, "providers", ["add"]))
    assert "cancelled" in result.output.plain
    assert ctx.config.custom_providers == []
    assert not __import__("os").path.exists("beeagent.json"), "nothing was written"


def test_validation_refusals_stop_the_interview_with_words_not_tracebacks(monkeypatch):
    ctx = _ctx()
    # a name nobody can type, then a sane one; a URL that is not one, then a good one
    _script(monkeypatch, ["Bad Name", "ok-name", "not-a-url",
                          "https://api.ok.test/v1", "k-abc12345", "", "m1"])
    result = asyncio.run(try_guided(ctx, "providers", ["add"]))
    assert "ok-name" in result.output.plain
    assert ctx.config.custom_providers[0].name == "ok-name"


# --- the picker the bare command opens ---------------------------------------------

def test_bare_providers_picker_offers_switches_only_to_real_doors():
    ctx = _ctx(_mine_config())
    title, values_fn, apply_cmd, current_fn = _picker_specs(ctx)["providers"]
    assert isinstance(title, str) and apply_cmd == "/providers"
    rows = dict(values_fn())
    assert {"g4f", "pool", "mine"} <= set(rows), rows
    assert "add" not in rows, "the picker chooses a value; `add` asks questions instead"
    assert current_fn() == ctx.config.provider


def test_an_unknown_endpoint_is_refused_without_an_agent_to_ask():
    """`ctx.agent` is not a permission to believe anything the line said.

    The registration check only ran when an `Agent` was in the context, so a
    dispatch without one — an embedder, a script, half of this very file — accepted
    `/providers nonsense`, wrote `provider: "nonsense"` into `beeagent.json`, and
    the next start asked a nonexistent endpoint for an answer. Refusing needs the
    names BeeCode actually serves, which is why this asserts both halves: junk is
    stopped, and `g4f`/`pool`/a saved custom endpoint all still pass.
    """
    from beeagent.ui.commands import dispatch

    config = _mine_config()
    ctx = _ctx(config)
    before = config.provider
    for line in ("/providers nonsense", "/providers use nonsense", "/provider nonsense"):
        result = dispatch(ctx, line)
        assert "no provider" in result.output.plain, (line, result.output.plain)
    assert config.provider == before, "the junk name was written anyway"

    assert dispatch(ctx, "/providers use g4f").output.plain.startswith("provider → g4f")
    assert dispatch(ctx, "/provider pool").output.plain.startswith("provider → pool")
    assert dispatch(ctx, "/providers use mine").output.plain.startswith("provider → mine")
