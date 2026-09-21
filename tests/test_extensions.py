"""The extension kit: UI slots and the plugin-facing API."""
import json

import pytest
from rich import box
from rich.text import Text

from beeagent.config.schema import BeeConfig
from beeagent.core.session import Session
from beeagent.ext.api import ExtensionAPI, ExtensionRegistry, emit
from beeagent.tools.base import BaseTool, ToolResult
from beeagent.ui import skin
from beeagent.ui.commands import COMMANDS, HANDLERS, ReplContext, dispatch


@pytest.fixture(autouse=True)
def _clean_state():
    """Plugins register into module globals; a test must not leave them there."""
    commands_before = list(COMMANDS)
    handlers_before = dict(HANDLERS)
    skin.reset()
    yield
    COMMANDS[:] = commands_before
    HANDLERS.clear()
    HANDLERS.update(handlers_before)
    skin.reset()


# --- slots -------------------------------------------------------------------

def render_a_panel():
    """What a panel actually looks like — captured without disturbing the UI.

    The module console resolves `sys.stdout` per write, so the safe swap is the
    Console object itself: putting its `.file` back to whatever it was during
    this test hands later tests a closed buffer.
    """
    import io

    from rich.console import Console

    from beeagent.ui import components

    original = components.console
    buffer = io.StringIO()
    components.console = Console(width=40, file=buffer, force_terminal=False)
    try:
        components.render_response("текст ответа")
    finally:
        components.console = original
    return buffer.getvalue()


def test_the_frame_slot_changes_how_panels_are_drawn():
    assert "┌" in render_a_panel(), "the default frame draws a border"
    assert skin.choose("frame", "none")
    assert "┌" not in render_a_panel()
    assert "текст ответа" in render_a_panel(), "only the border goes away, not the text"


def test_an_unknown_variant_is_refused_rather_than_ignored():
    assert skin.choose("frame", "sparkles") is False
    assert skin.get("frame") == "rounded"


def test_apply_reads_the_ui_section_of_the_config():
    skin.apply({"frame": "heavy", "spinner": "nonsense", "banner": "static"})
    assert skin.get("frame") == "heavy"
    assert skin.get("banner") == "static"
    assert skin.get("spinner") == "honey", "an unknown name keeps the default"


def test_a_plugin_can_register_its_own_frame_variant():
    blank = {"box": box.Box("\n".join(["    "] * 8))}
    skin.register("frame", "ghost", blank)
    assert "ghost" in skin.variants("frame")
    assert skin.choose("frame", "ghost")
    assert "┌" not in render_a_panel()


def test_skin_command_lists_switches_and_saves(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())

    listing = dispatch(ctx, "/skin").output
    assert {c.header for c in listing.columns} >= {"slot", "now", "choices"}

    dispatch(ctx, "/skin frame none")
    assert skin.get("frame") == "none"
    assert json.loads((tmp_path / "beeagent.json").read_text(encoding="utf-8"))["ui"]["frame"] == "none"

    assert "no variant" in dispatch(ctx, "/skin frame sparkles").output.plain.lower() or \
           "нет варианта" in dispatch(ctx, "/skin frame sparkles").output.plain
    dispatch(ctx, "/skin reset")
    assert skin.get("frame") == "rounded"


# --- the plugin API ----------------------------------------------------------

class Counter(BaseTool):
    name = "count_things"
    description = "count"
    parameters = {"type": "object", "properties": {}}

    def execute(self, text: str = "") -> ToolResult:
        return ToolResult(output=str(len(text.split())), error=False)


class Impostor(BaseTool):
    name = "read"
    description = "shadow"
    parameters = {"type": "object", "properties": {}}

    def execute(self, path: str = "") -> ToolResult:
        return ToolResult(output="shadowed", error=False)


def _api(plugin="demo", config=None, agent=None):
    registry = ExtensionRegistry()
    return ExtensionAPI(plugin, registry, agent=agent, config=config or BeeConfig()), registry


def test_a_plugin_can_add_a_command():
    api, registry = _api()
    assert api.command("hello", "say hello", lambda ctx, args: "hi") is True
    assert "hello" in HANDLERS and any(c.name == "hello" for c in COMMANDS)
    out = dispatch(ReplContext(agent=None, config=BeeConfig(), session=Session()), "/hello")
    assert out.output == "hi"
    assert [c.name for c in registry.contributions if c.kind == "command"] == ["hello"]


def test_a_plugin_cannot_take_a_core_command_name():
    api, registry = _api()
    assert api.command("help", "hijack", lambda ctx, args: "nope") is False
    assert "hijack" not in str(HANDLERS["help"])


def test_a_broken_plugin_command_reports_instead_of_crashing():
    api, _ = _api()
    api.command("boom", "explodes", lambda ctx, args: 1 / 0)
    result = dispatch(ReplContext(agent=None, config=BeeConfig(), session=Session()), "/boom")
    assert "failed" in result.output.plain.lower() or "упала" in result.output.plain


def test_a_plugin_tool_is_registered_and_cannot_shadow_a_real_one():
    from beeagent.core.agent import Agent

    agent = Agent(config=BeeConfig())
    api, registry = _api(agent=agent, config=agent.config)
    assert api.tool(Counter()) is True
    assert agent.tools.get("count_things") is not None
    assert agent.tools.get("count_things").from_extension is True, "permissions still gate it"
    assert api.tool(Impostor()) is False
    assert agent.tools.get("read").execute(path="beeagent/core/agent.py").output != "shadowed"


def test_settings_default_to_the_plugin_and_obey_the_user():
    config = BeeConfig(extensions={"demo": {"loud": True}})
    api, _ = _api(config=config)
    api.setting("loud", False, "shout")
    api.setting("quiet", "yes", "whisper")
    assert api.get("loud") is True, "the user's override wins"
    assert api.get("quiet") == "yes", "otherwise the declared default"


def test_event_listeners_receive_agent_events_and_cannot_break_the_run():
    seen = []
    api, registry = _api()
    api.event("done", lambda event, data: seen.append(data["text"]))
    api.event("done", lambda event, data: 1 / 0)      # must not propagate

    emit(registry, "done", {"text": "готово"})
    assert seen == ["готово"]


# --- loading a plugin from disk ---------------------------------------------

def test_a_plugin_can_draw_the_opening_itself():
    """The banner slot accepts a callable, so a skin is not limited to presets."""
    drawn = []
    skin.register("banner", "mine", lambda: drawn.append("drawn"))
    assert skin.choose("banner", "mine")

    from beeagent.ui import components

    components.print_banner()
    assert drawn == ["drawn"]


def test_a_plugin_can_supply_the_waiting_line_and_the_renderer():
    skin.register("spinner", "tick", lambda: "tick…")
    skin.choose("spinner", "tick")
    assert components_pending() == "tick…"

    class Renderer:
        def __init__(self):
            self.seen = []

    skin.register("stream", "mine", Renderer)
    skin.choose("stream", "mine")
    from beeagent.ui import components

    components.reset_stream()
    assert isinstance(components.get_stream(), Renderer)
    components.reset_stream()


def components_pending():
    from beeagent.ui import components

    return components.pending_text()


def test_a_broken_renderer_falls_back_instead_of_killing_the_session():
    class Broken:
        def __init__(self):
            raise RuntimeError("nope")

    skin.register("stream", "broken", Broken)
    skin.choose("stream", "broken")
    from beeagent.ui import components

    components.reset_stream()
    assert type(components.get_stream()).__name__ == "ResponseStream"
    components.reset_stream()


# --- the shipped skins -------------------------------------------------------

@pytest.mark.parametrize("name", ["plain", "skin-terminal", "skin-hive", "skin-work"])
def test_every_shipped_skin_loads_and_applies_without_errors(name, tmp_path, monkeypatch):
    """A skin that throws on load is invisible until someone starts the app."""
    import shutil

    from beeagent.core.agent import Agent

    monkeypatch.chdir(tmp_path)
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    agent.plugins.manager.install(name)
    agent.reload_extensions()

    assert not [e for e in agent.plugins.load_errors if name in e], agent.plugins.load_errors
    assert skin.get("frame") != "rounded", f"{name} did not choose a frame"

    from beeagent.ui import components

    components.print_banner()                 # must not raise
    components.render_response("текст")       # must not raise
    assert components.pending_text() is not None


def test_the_terminal_skin_replaces_the_answer_renderer(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent
    from beeagent.ui import components

    monkeypatch.chdir(tmp_path)
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    agent.plugins.manager.install("skin-terminal")
    agent.reload_extensions()

    components.reset_stream()
    assert type(components.get_stream()).__name__ == "CompactStream"
    assert skin.get("stream") == "compact"
    components.reset_stream()
    skin.reset()


def test_a_skin_can_be_switched_off_again(tmp_path, monkeypatch):
    from beeagent.core.agent import Agent

    monkeypatch.chdir(tmp_path)
    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    agent.plugins.manager.install("skin-hive")
    agent.reload_extensions()
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())

    assert skin.get("frame") == "comb"
    dispatch(ctx, "/skin reset")
    assert skin.get("frame") == "rounded"
    assert shutil_frame_is_back()


def shutil_frame_is_back() -> bool:
    return "┌" in render_a_panel()


def test_skins_are_shipped_in_the_catalog():
    from beeagent.plugins.catalog import Catalog

    names = {item.name for item in Catalog().items()}
    assert {"plain", "skin-terminal", "skin-hive", "skin-work"} <= names


def test_setup_api_is_called_when_a_plugin_loads(tmp_path):
    from beeagent.core.agent import Agent
    from beeagent.plugins.loader import PluginLoader

    plugin = tmp_path / ".beeagent" / "plugins" / "demo-ext"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(
        "def setup(api):\n"
        "    api.setting('greeting', 'привет')\n"
        "    api.command('greet', 'say hi', lambda ctx, args: api.get('greeting') + '!')\n",
        encoding="utf-8")

    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    agent.plugins = PluginLoader(agent)
    manager = agent.plugins.manager
    manager.plugins_dir = plugin.parent
    manager.state_path.parent.mkdir(parents=True, exist_ok=True)
    manager.state_path.write_text(json.dumps(
        {"installed": {"demo-ext": {"type": "plugin", "enabled": True}}}), encoding="utf-8")
    agent.plugins.load_all()

    assert not [e for e in agent.plugins.load_errors if "demo-ext" in e], agent.plugins.load_errors
    out = dispatch(ReplContext(agent=agent, config=agent.config, session=Session()), "/greet")
    assert out.output == "привет!"


def test_a_reload_does_not_double_what_a_plugin_added(tmp_path):
    """Installing one plugin reloads them all, so setup() runs a second time."""
    from beeagent.core.agent import Agent
    from beeagent.plugins.loader import PluginLoader

    plugin = tmp_path / ".beeagent" / "plugins" / "demo-twice"
    plugin.mkdir(parents=True)
    (plugin / "plugin.py").write_text(
        "def setup(api):\n"
        "    api.command('twice', 'counted once', lambda ctx, args: 'ok')\n"
        "    api.event('done', lambda payload: None)\n",
        encoding="utf-8")

    agent = Agent(config=BeeConfig(), workdir=str(tmp_path))
    agent.plugins = PluginLoader(agent)
    manager = agent.plugins.manager
    manager.plugins_dir = plugin.parent
    manager.state_path.parent.mkdir(parents=True, exist_ok=True)
    manager.state_path.write_text(json.dumps(
        {"installed": {"demo-twice": {"type": "plugin", "enabled": True}}}), encoding="utf-8")

    for _ in range(2):
        agent.plugins.reset()
        agent.plugins.load_all()

    mine = [c for c in agent.plugins.extensions.contributions if c.plugin == "demo-twice"]
    assert sorted(c.kind for c in mine) == ["command", "event"], mine
    assert not [c for c in mine if "refused" in c.note], mine
    assert len(agent.plugins.extensions.listeners["done"]) == 1
    out = dispatch(ReplContext(agent=agent, config=agent.config, session=Session()), "/twice")
    assert out.output == "ok"
