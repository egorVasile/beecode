"""Extension system: catalog, install/remove, loader wiring, MCP round-trip.

Extensions are discovered in `./.beeagent`, so every test here works in a folder
of its own — and hands the process cwd back itself: conftest checks the cwd at
teardown, and an autouse fixture that leaves the restoring to `monkeypatch` makes
every test in the file error after passing.
"""
import json
import os
import sys
from pathlib import Path

import pytest

from beeagent.plugins.catalog import CATALOG_PATH, TEMPLATES_DIR, Catalog
from beeagent.plugins.loader import PluginLoader, strip_frontmatter
from beeagent.plugins.mcp import McpManager, McpStdioClient
from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.ui.commands import ReplContext, dispatch

FAKE_SERVER = """
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
print("fake mcp server up", flush=True)  # noise line the client must skip
for line in sys.stdin:
    line = line.strip()
    if not line or not line.startswith("{"):
        continue
    msg = json.loads(line)
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "1.0"}}}
    elif method == "tools/list":
        out = {"jsonrpc": "2.0", "id": mid, "result": {"tools": [
            {"name": "echo", "description": "Эхо: возвращает текст",
             "inputSchema": {"type": "object",
                             "properties": {"text": {"type": "string"}}}}]}}
    elif method == "tools/call":
        text = msg.get("params", {}).get("arguments", {}).get("text", "")
        out = {"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": "echo: " + text}]}}
    elif mid is None:
        continue  # notification
    else:
        out = {"jsonrpc": "2.0", "id": mid,
               "error": {"code": -32601, "message": "Method not found"}}
    print(json.dumps(out), flush=True)
"""


@pytest.fixture(autouse=True)
def isolated_cwd(tmp_path):
    """Extensions live in ./.beeagent — work in a folder of our own, and hand it back.

    The restore is this fixture's own job: conftest's `_restore_cwd` checks the
    process cwd at teardown, and the undo `monkeypatch.chdir` registers runs after
    that check, so leaving it to monkeypatch errored every test in this file on a
    passed assertion.
    """
    before = os.getcwd()
    os.chdir(str(tmp_path))
    try:
        yield tmp_path
    finally:
        os.chdir(before)


@pytest.fixture
def agent():
    return Agent(config=BeeConfig())


# --- catalog ----------------------------------------------------------------

def test_catalog_items_are_wellformed():
    catalog = Catalog(CATALOG_PATH)
    items = catalog.items()
    assert len(items) >= 15
    for item in items:
        assert item.name and item.description
        assert item.type in ("skill", "plugin", "mcp")
        kind = item.source.get("kind")
        if kind == "builtin":
            assert (TEMPLATES_DIR / item.source["path"]).is_dir(), item.name
        elif kind == "mcp":
            assert item.source.get("command"), item.name
        else:
            pytest.fail(f"{item.name}: unexpected source kind {kind!r}")


def test_catalog_covers_every_kind():
    types = {item.type for item in Catalog(CATALOG_PATH).items()}
    assert types == {"skill", "plugin", "mcp"}


def test_catalog_search_and_get():
    catalog = Catalog(CATALOG_PATH)
    assert catalog.get("code-review").type == "skill"
    assert catalog.get("nope") is None
    assert any(i.name == "code-review" for i in catalog.search("review"))
    git = catalog.find_git("https://example.com/some/thing.git")
    assert git.name == "thing" and git.source["kind"] == "git"


# --- manager -------------------------------------------------------------------

def test_install_and_uninstall_builtin_skill():
    from beeagent.plugins.manager import PluginManager
    manager = PluginManager()
    report = manager.install("code-review")
    assert report == {"name": "code-review", "type": "skill",
                      "description": report["description"]}
    assert (Path(".beeagent/plugins/code-review/SKILL.md")).is_file()
    assert manager.is_installed("code-review")
    assert manager.installed_skill_dirs()

    manager.uninstall("code-review")
    assert not manager.is_installed("code-review")
    assert not Path(".beeagent/plugins/code-review").exists()


def test_install_mcp_writes_server_config():
    from beeagent.plugins.manager import PluginManager
    manager = PluginManager()
    manager.install("filesystem")
    servers = manager.mcp_servers()
    assert servers["filesystem"]["command"] == "npx"
    assert manager.installed()["filesystem"]["type"] == "mcp"

    manager.uninstall("filesystem")
    assert "filesystem" not in manager.mcp_servers()


def test_enable_disable_controls_discovery():
    from beeagent.plugins.manager import PluginManager
    manager = PluginManager()
    manager.install("code-review")
    assert manager.installed_skill_dirs()
    manager.set_enabled("code-review", False)
    assert not manager.installed_skill_dirs()
    assert not manager.is_enabled("code-review")


def test_nested_extension_dirs_are_discovered():
    """A cloned repo usually nests its content: <install>/skills/<name>/SKILL.md."""
    from beeagent.plugins.manager import PluginManager
    nested = Path(".beeagent/plugins/thing/skills/deep-skill")
    nested.mkdir(parents=True)
    (nested / "SKILL.md").write_text("---\nname: deep-skill\n---\n# Do it\n",
                                     encoding="utf-8")

    manager = PluginManager()
    state = manager._state()
    state["installed"]["thing"] = {"type": "plugin", "enabled": True}
    manager._save_state(state)

    assert [d.name for d in manager.installed_skill_dirs()] == ["deep-skill"]


def test_install_unknown_target_raises():
    from beeagent.plugins.manager import PluginManager
    with pytest.raises(ValueError):
        PluginManager().install("definitely-not-in-the-catalog")


def test_add_mcp_server_by_command():
    from beeagent.plugins.manager import PluginManager
    manager = PluginManager()
    manager.add_mcp_server("local", sys.executable, ["server.py"])
    cfg = manager.mcp_servers()["local"]
    assert cfg["command"] == sys.executable and cfg["args"] == ["server.py"]


# --- loader wiring -----------------------------------------------------------

def test_loader_registers_skill_tool_even_with_nothing_installed(agent):
    assert "skill" in agent.tools.list_names()
    assert agent.plugins.skills == []
    assert agent.plugins.load_errors == []
    assert agent.plugins.skills_prompt_section() == ""


def test_loader_installs_skill_and_plugin_tools(agent):
    agent.plugins.manager.install("code-review")
    agent.plugins.manager.install("json-tool")
    errors = agent.reload_extensions()
    assert errors == []
    assert "json_format" in agent.tools.list_names()
    assert [s.name for s in agent.plugins.skills] == ["code-review"]
    assert "# SKILLS" in agent.plugins.skills_prompt_section()
    assert "code-review" in agent.context.skills_section


def test_reload_removes_tools_of_uninstalled_plugin(agent):
    agent.plugins.manager.install("json-tool")
    agent.reload_extensions()
    assert "json_format" in agent.tools.list_names()

    agent.plugins.manager.uninstall("json-tool")
    agent.reload_extensions()
    assert "json_format" not in agent.tools.list_names()


def test_skill_tool_lists_then_loads(agent):
    agent.plugins.manager.install("code-review")
    agent.reload_extensions()
    tool = agent.tools.get("skill")

    listed = tool.execute(action="list")
    assert not listed.error and "code-review" in listed.output

    loaded = tool.execute(action="load", name="code-review")
    assert not loaded.error and "name: code-review" in loaded.output

    missing = tool.execute(action="load", name="nope")
    assert missing.error


def test_strip_frontmatter():
    assert strip_frontmatter("---\nname: x\n---\n# Hi\n") == "# Hi\n"
    assert strip_frontmatter("# No header") == "# No header"


# --- MCP client --------------------------------------------------------------

@pytest.fixture
def fake_server_path(tmp_path):
    path = tmp_path / "fake_mcp_server.py"
    path.write_text(FAKE_SERVER, encoding="utf-8")
    return path


async def _list_and_call(fake_server_path):
    client = McpStdioClient("fake", sys.executable, [str(fake_server_path)])
    await client.start()
    try:
        tools = await client.list_tools()
        text = await client.call_tool("echo", {"text": "привет"})
    finally:
        await client.stop()
    return tools, text


def test_stdio_client_handshake_and_call(fake_server_path):
    import asyncio
    tools, text = asyncio.run(_list_and_call(fake_server_path))
    assert tools[0]["name"] == "echo"
    assert text == "echo: привет"  # UTF-8 survived the round trip


def test_manager_caches_tools_and_loader_registers_them(agent, fake_server_path):
    manager = agent.plugins.manager
    manager.add_mcp_server("fake", sys.executable, [str(fake_server_path)])
    assert agent.reload_extensions() == []
    assert "fake" in agent.plugins.pending_mcp  # not connected yet

    tools = agent.plugins.connect_mcp("fake")
    assert [t["name"] for t in tools] == ["echo"]
    assert "mcp_fake_echo" in agent.tools.list_names()
    assert "fake" not in agent.plugins.pending_mcp

    result = agent.tools.get("mcp_fake_echo").execute(text="мяу")
    assert not result.error and result.output == "echo: мяу"


def test_connected_tools_survive_reload(agent, fake_server_path):
    manager = agent.plugins.manager
    manager.add_mcp_server("fake", sys.executable, [str(fake_server_path)])
    agent.plugins.connect_mcp("fake")
    agent.reload_extensions()
    assert "mcp_fake_echo" in agent.tools.list_names()


def test_discover_reports_unreachable_server(tmp_path):
    manager = McpManager(config_path=tmp_path / "mcp.json",
                         cache_path=tmp_path / "cache.json")
    Path(tmp_path / "mcp.json").write_text(json.dumps({"servers": {
        "broken": {"command": sys.executable, "args": ["-c", "raise SystemExit(1)"],
                   "enabled": True}}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="unreachable"):
        manager.discover_tools_blocking("broken", timeout=15.0)


# --- slash commands -----------------------------------------------------------

def _run(ctx, line):
    return dispatch(ctx, line)


def _text(renderable) -> str:
    """Render a rich object to plain text so assertions can grep it."""
    import io
    from rich.console import Console
    buf = io.StringIO()
    Console(file=buf, width=200).print(renderable)
    return buf.getvalue()


def test_plugins_command_lists_catalog(agent):
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    full = _run(ctx, "/plugins")
    assert full.output.row_count == len(agent.plugins.manager.catalog.items())
    filtered = _run(ctx, "/plugins skills")
    assert 0 < filtered.output.row_count < full.output.row_count
    assert "Nothing found" in _run(ctx, "/plugins zzz-nothing").output.plain


def test_plugin_install_command_reloads_agent(agent):
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    text = _run(ctx, "/plugin install code-review").output.plain
    assert "installed" in text and "code-review" in text
    assert [s.name for s in agent.plugins.skills] == ["code-review"]
    assert "✅ installed" in _text(_run(ctx, "/plugin list").output)


def test_plugin_remove_and_toggle_commands(agent):
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    _run(ctx, "/plugin install code-review")
    assert "disabled" in _run(ctx, "/plugin disable code-review").output.plain
    assert agent.plugins.skills == []
    assert "enabled" in _run(ctx, "/plugin enable code-review").output.plain
    assert [s.name for s in agent.plugins.skills] == ["code-review"]
    assert "removed" in _run(ctx, "/plugin remove code-review").output.plain
    assert agent.plugins.skills == []


def test_plugin_commands_without_agent_still_work(tmp_path):
    # `beecode plugins install <name>` runs with no agent in play.
    ctx = ReplContext()
    assert "installed" in _run(ctx, "/plugin install notes").output.plain
    assert "notes" in _text(_run(ctx, "/plugin list").output)


def test_plugin_install_reports_failure(agent):
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    assert "could not install" in _run(ctx, "/plugin install nope-not-here").output.plain


def test_skill_command_shows_instructions(agent):
    from rich.panel import Panel
    from rich.markdown import Markdown
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    _run(ctx, "/plugin install code-review")
    result = _run(ctx, "/skill code-review")
    assert isinstance(result.output, Panel)
    assert isinstance(result.output.renderable, Markdown)
    skill = agent.plugins.skills[0]
    assert not skill.body().startswith("---") and skill.body() != skill.content()
    assert "Skill" in _run(ctx, "/skill missing").output.plain
    assert "Skills" in _run(ctx, "/skills").output.title


def test_mcp_commands_report_connect_state(agent, fake_server_path):
    ctx = ReplContext(agent=agent, config=agent.config, session=Session())
    agent.plugins.manager.add_mcp_server("fake", sys.executable,
                                         [str(fake_server_path)])
    agent.reload_extensions()
    assert agent.plugins.pending_mcp == ["fake"]  # schema cache is still empty

    assert "connected 1 tools" in _run(ctx, "/mcp connect fake").output.plain
    assert agent.plugins.pending_mcp == []
    assert "echo" in _text(_run(ctx, "/mcp tools fake").output)
    listing = _text(_run(ctx, "/mcp list").output)
    assert "fake" in listing and "🟢 ready" in listing  # connected, cached
    assert "usage: /mcp" in _run(ctx, "/mcp bogus").output.plain


def test_lang_command_switches_the_interface():
    """The same command answers in the other language after /lang ru."""
    from beeagent.i18n import get_lang, set_lang

    ctx = ReplContext(agent=None, config=BeeConfig(), session=Session())
    set_lang("en")
    assert "current language" in _run(ctx, "/lang").output.plain
    set_lang("ru")
    assert "текущий язык" in _run(ctx, "/lang").output.plain
    assert _run(ctx, "/lang en").output.plain.startswith("interface language")
    assert get_lang() == "en"
