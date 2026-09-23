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
    """With no agent built, the list shown is the g4f catalogue."""
    from beeagent.providers.g4f_provider import G4fProvider

    models = available_models(ctx)
    assert models == G4fProvider.discover_models()
    assert "command-a-03-2025" in models, "the measured keyless default is offered"


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
    dispatch(ctx, "/model command-a-03-2025")
    assert ctx.config.model == "command-a-03-2025"


def test_dispatch_model_unknown_does_not_switch(ctx):
    before = ctx.config.model
    dispatch(ctx, "/model does-not-exist")
    assert ctx.config.model == before, "an unknown name must not clobber the model"


def test_a_model_name_with_a_space_is_reachable_and_remembered(tmp_path, monkeypatch):
    """The picker sends the whole id; args[0] used to cut "Think Deeper" in half.

    The shipped catalogue has no spaced id in it today, so one is injected: this
    is about the command rejoining its arguments, not about what upstream lists.
    """
    monkeypatch.chdir(tmp_path)
    from beeagent.providers.g4f_provider import G4fProvider

    spaced = "Think Deeper 3.5"
    monkeypatch.setattr(G4fProvider, "discover_models",
                        classmethod(lambda cls: [spaced, "command-a-03-2025"]))
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    assert spaced in available_models(ctx)

    dispatch(ctx, f"/model {spaced}")
    assert ctx.config.model == spaced
    assert (tmp_path / "beeagent.json").exists(), "the choice has to survive a restart"


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


def test_readme_command_table_matches_the_registry():
    """The table is generated — a forgotten /key must fail here, not in review."""
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(root / "scripts"))
    from sync_readme import BEGIN, END, build_table

    text = (root / "README.md").read_text(encoding="utf-8")
    assert BEGIN in text and END in text
    documented = text.split(BEGIN, 1)[1].split(END, 1)[0].strip()
    assert documented == build_table().strip(), "run: python scripts/sync_readme.py"
