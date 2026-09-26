import pytest

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ui.commands import (
    COMMANDS, HANDLERS, ReplContext,
    get_suggestions, build_sources, dispatch,
    available_models, available_providers, model_choices,
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
def ctx(tmp_path, monkeypatch):
    # Commands that change settings write `beeagent.json` where the process
    # stands, not where the config object points; without this a suite run leaves
    # a fake config in the checkout and the next developer reads it as theirs.
    monkeypatch.chdir(tmp_path)
    return ReplContext(agent=None, config=BeeConfig(), session=Session())


def test_available_models_falls_back_to_g4f(ctx):
    """With no agent built, the list shown is the g4f catalogue."""
    from beeagent.providers.g4f_provider import G4fProvider

    models = available_models(ctx)
    assert models == G4fProvider.discover_models()
    assert "command-a-03-2025" in models, "the measured keyless default is offered"


def test_available_providers_includes_builtins(ctx):
    """Two providers are offered, and both answer without anyone holding a key.

    The list used to carry every free-tier endpoint that takes a key of your own,
    which contradicts what BeeCode is advertised as; a stored key still works via
    `/provider <name>`, it is simply no longer offered to a stranger.
    """
    providers = available_providers(ctx)
    assert providers == ["g4f", "pool"], providers
    for name in ("groq", "openrouter", "crax", "ollama", "openai_compat"):
        assert name not in providers, f"{name} needs a key and must not be offered"


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


def _pool_ctx(url="https://pool.that.will.not.answer"):
    """A context on the pool provider, with a seat token that cannot reach it."""
    from beeagent.core.agent import Agent

    config = BeeConfig(provider="pool", pool_url=url, pool_token="seattoken")
    return ReplContext(agent=Agent(config=config), config=config, session=Session())


def test_the_pool_still_offers_its_models_when_the_box_cannot_be_reached(tmp_path,
                                                                         monkeypatch):
    """A free instance sleeps; an empty picker reads as "the pool has no models".

    That sent a person to change provider while the box was merely waking up. The
    measured list ships with the client for exactly this case — and the run happens
    in its own folder, because `.beeagent/models_<name>.json` resolves against the
    process cwd: a list left in the developer's tree by yesterday's probe is not
    something a test of the shipped fallback should be reading.
    """
    from beeagent.providers.pool import PoolProvider

    monkeypatch.chdir(tmp_path)
    rows = model_choices(_pool_ctx())
    assert rows, "the picker must not be empty because one request failed"
    assert [r[0] for r in rows] == PoolProvider.models
    assert PoolProvider.models[0] in rows[0][1]


def test_a_non_g4f_picker_labels_the_window_like_every_other():
    """The rows used to be bare names, so another provider looked like a dumber screen."""
    rows = model_choices(_pool_ctx())
    label = rows[0][1]
    assert "k" in label or "M" in label, f"no window on the row: {label!r}"


def test_a_refused_provider_switch_says_which_provider_is_still_active():
    """`/provider crax` with no key leaves you on g4f -- and the model list is
    then g4f's, which one real user read as crax listing the wrong models."""
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    answer = dispatch(ctx, "/provider crax")
    text = str(getattr(answer, "output", "")) + str(getattr(answer, "error", "") or "")
    assert ctx.config.provider == "g4f", "the switch must not happen"
    assert "g4f" in text.split("still on")[-1] or "ты всё ещё" in text, \
        f"the message must name what is still active: {text!r}"


def test_a_freshly_enrolled_seat_reaches_the_running_provider():
    """The Agent builds its providers once, so a token written afterwards landed
    nowhere: every message said "no seat token yet — /pool enroll" until restart.
    """
    from beeagent.core.agent import Agent
    from beeagent.providers.pool import PoolProvider

    config = BeeConfig(provider="pool", pool_url="https://pool.example")
    agent = Agent(config=config)
    live = PoolProvider(url="", token="")          # built before the seat existed
    agent.providers.register(live, replace=True)
    ctx = ReplContext(agent=agent, config=config, session=Session())

    config.pool_token = "seat-token-just-minted"
    from beeagent.ui.commands import _pool_provider_refresh
    _pool_provider_refresh(ctx)

    assert live.token == "seat-token-just-minted", "the running provider must see the seat"
    assert live.url == "https://pool.example"


def test_switching_provider_back_to_g4f_moves_the_model_with_it(tmp_path, monkeypatch):
    """The reset used to happen only when leaving g4f.

    `/provider pool` then `/provider g4f` left the session asking g4f for a crax
    model id, which is the same dead request in the other direction.
    """
    from beeagent.core.agent import Agent

    monkeypatch.chdir(tmp_path)
    config = BeeConfig(provider="pool", pool_url="https://pool.example", pool_token="t")
    agent = Agent(config=config)
    ctx = ReplContext(agent=agent, config=config, session=Session())
    dispatch(ctx, "/provider pool")
    shipped = agent.providers.get("pool").models[0]
    assert config.model == shipped, config.model
    dispatch(ctx, "/provider g4f")
    assert config.model != shipped, "g4f cannot serve a crax model id"
    assert config.model in available_models(ctx)


def test_a_model_list_saved_by_another_version_is_not_used(tmp_path, monkeypatch):
    """`beecode --update` can change what a provider answers for.

    The hour-old cache had no version in it, so after an update the picker kept
    showing the previous build's list and a fixed catalogue looked unfixed.
    """
    import json
    from beeagent.ui import commands

    monkeypatch.chdir(tmp_path)
    path = tmp_path / ".beeagent" / "models_groq.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"saved_at": __import__("time").time(),
                                "version": "0.0.0-not-this-build",
                                "models": ["stale-model"]}), encoding="utf-8")

    async def fresh():
        return ["fresh-model"]

    got = commands._cached_models("groq", fresh, allow_fetch=True,
                                  fallback=["declared"])
    assert got == ["fresh-model"], got


def test_stop_reaches_the_agent_and_reports_whether_it_was_running():
    """`/stop` must be a request the loop honours, not a message that prints."""
    from beeagent.core.agent import Agent

    config = BeeConfig()
    agent = Agent(config=config)
    ctx = ReplContext(agent=agent, config=config, session=Session())

    result = dispatch(ctx, "/stop")
    assert result.action == "stop"
    # The flag is how a worker thread is stopped, but arming it while nothing runs
    # used to break the NEXT question: run() saw a stop it never asked for and
    # answered "Stopped by you" with zero requests sent.
    assert agent.stop_requested is False, "an idle /stop must not arm the next turn"
    assert "nothing is running" in str(result.output.plain).lower() or \
           "ничего не выполняется" in str(result.output.plain)


def test_a_stopped_turn_ends_the_run_instead_of_continuing(tmp_path, monkeypatch):
    """The flag has to end the loop at the next turn, or it is decoration."""
    import asyncio

    from beeagent.core.agent import Agent

    # The provider name must be the one the config selects. Registering a fake
    # under another name leaves run() talking to the real g4f endpoint -- which
    # is a network call inside a test, and it passes for the wrong reason.
    config = BeeConfig(provider="silent")
    agent = Agent(config=config)

    class Silent:
        name = "silent"
        models = ["m"]

        async def chat(self, messages, model="m", stream=False):
            agent.request_stop()          # the user pressed /stop during turn 1
            # A tool call, not a final sentence: with a plain answer the run is
            # over on turn 1 and there is nothing left to stop.
            return '```json\n{"tool": "todo", "args": {"action": "list"}}\n```'

    agent.providers.register(Silent(), replace=True)
    assert agent.providers.get("silent") is not None
    monkeypatch.chdir(tmp_path)
    answer = asyncio.run(agent.run("say something long"))
    assert answer == "Stopped by you", answer
    assert agent.stop_requested is False, "the flag must not leak into the next run"


def test_the_todo_tool_refuses_to_write_the_same_task_twice(tmp_path, monkeypatch):
    """A model told to "record the plan" re-added three tasks nine times each.

    The list reached 27 entries with nothing marked done, which is worse than no
    list: /tasks became noise. Adding the same text again returns the existing id
    instead of growing the file.
    """
    import os

    from beeagent.tools.todo import TodoTool

    monkeypatch.chdir(tmp_path)
    os.makedirs(".beeagent", exist_ok=True)
    tool = TodoTool()

    first = tool.execute("add", text="List what is in the folder")
    again = tool.execute("add", text="  list WHAT is in the folder  ")
    third = tool.execute("add", text="Read the files")

    assert not first.error and not again.error
    assert "already says this" in again.output, again.output
    listed = tool.execute("list")
    assert listed.metadata["count"] == 2, listed.output
    assert not third.error
