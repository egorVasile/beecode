import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ui.commands import (
    COMMANDS, HANDLERS, ReplContext,
    get_suggestions, build_sources, dispatch,
    available_models, available_providers,
)

SOURCES = {
    "model": ["gpt-4", "gpt-4o", "claude-3.5-sonnet"],
    "provider": ["g4f", "ollama"],
    "mode": ["normal", "economy"],
    "session": ["20260918_120000"],
}


def test_command_registry_has_core_commands():
    names = {c.name for c in COMMANDS}
    for expected in ("help", "model", "models", "provider", "mode", "quit", "clear", "reset"):
        assert expected in names
    # every command has a handler
    for c in COMMANDS:
        assert c.name in HANDLERS


def test_slash_lists_all_commands():
    sugg = get_suggestions("/", SOURCES)
    names = {s.text for s in sugg}
    assert names == {"/" + c.name for c in COMMANDS}
    assert all(s.start_position == -1 for s in sugg)


def test_slash_filters_by_prefix():
    sugg = get_suggestions("/mo", SOURCES)
    assert {s.text for s in sugg} == {"/model", "/models", "/mode"}


def test_non_slash_returns_nothing():
    assert get_suggestions("hello", SOURCES) == []
    assert get_suggestions("", SOURCES) == []


def test_argument_completion_for_model():
    sugg = get_suggestions("/model gpt", SOURCES)
    assert {s.text for s in sugg} == {"gpt-4", "gpt-4o"}
    assert all(s.start_position == -len("gpt") for s in sugg)


def test_argument_completion_trailing_space_shows_all():
    sugg = get_suggestions("/mode ", SOURCES)
    assert {s.text for s in sugg} == {"normal", "economy"}
    assert all(s.start_position == 0 for s in sugg)


def test_command_without_arg_has_no_arg_completion():
    assert get_suggestions("/help x", SOURCES) == []


def test_unknown_command_no_completion():
    assert get_suggestions("/nope ", SOURCES) == []


@pytest.fixture
def ctx():
    return ReplContext(agent=None, config=BeeConfig(), session=Session())


def test_available_models_falls_back_to_g4f(ctx):
    models = available_models(ctx)
    assert "gpt-4" in models


def test_available_providers_includes_builtins(ctx):
    providers = available_providers(ctx)
    for p in ("g4f", "openai_compat", "ollama"):
        assert p in providers


def test_build_sources_keys(ctx):
    src = build_sources(ctx)
    # Core contract keys must exist; extra sources (theme/path) are allowed
    # because live completion for /theme and path-based commands uses them.
    assert {"model", "provider", "mode", "session"}.issubset(src.keys())


def test_dispatch_model_switch(ctx):
    dispatch(ctx, "/model gpt-4o")
    assert ctx.config.model == "gpt-4o"


def test_dispatch_model_unknown_does_not_switch(ctx):
    dispatch(ctx, "/model does-not-exist")
    assert ctx.config.model == "gpt-4"


def test_dispatch_mode_switch(ctx):
    dispatch(ctx, "/mode economy")
    assert ctx.config.mode == "economy"


def test_dispatch_reset_creates_new_session(ctx):
    old = ctx.session.session_id
    dispatch(ctx, "/reset")
    assert isinstance(ctx.session, Session)


def test_dispatch_quit_sets_flag(ctx):
    assert ctx.running is True
    dispatch(ctx, "/quit")
    assert ctx.running is False


def test_dispatch_help_and_config_do_not_crash(ctx):
    dispatch(ctx, "/help")
    dispatch(ctx, "/config")
    dispatch(ctx, "/models")
    dispatch(ctx, "/providers")
    dispatch(ctx, "/sessions")


def test_dispatch_unknown_command_is_safe(ctx):
    dispatch(ctx, "/bogus")


def test_completer_yields_completions(ctx):
    from beeagent.ui.repl import BeeCompleter

    class _Doc:
        text_before_cursor = "/mo"

    completer = BeeCompleter(ctx)
    comps = list(completer.get_completions(_Doc(), None))
    assert {c.text for c in comps} == {"/model", "/models", "/mode"}
