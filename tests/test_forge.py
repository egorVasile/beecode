"""The model extending itself: draft, validate, install, use — no restart.

`ctxbar` below is the shape every forged pack takes: a folder the `write`
tool can produce, `plugin.py` plus `plugin.json`, validated without execution
and loaded through the same gate as a downloaded pack. The loop is driven
here the way the model drives it (write → forge validate → forge install →
dispatch), including the trust half: the folder is never `/trust`ed, and the
install pin is what lets the bytes run.
"""
import io
import json

import pytest
from rich.console import Console

from beeagent.config.schema import BeeConfig
from beeagent.core.agent import Agent
from beeagent.core.session import Session
from beeagent.plugins.validate import validate_staged
from beeagent.tools.forge import ForgeTool
from beeagent.ui.commands import ReplContext, dispatch

CTXBAR_PLUGIN = '''"""Context-fill bar, forged live: /ctxbar prints the window as a colored bar."""

GREEN_LIMIT = 50
YELLOW_LIMIT = 80


def setup(api):
    def ctxbar(ctx, args):
        agent = getattr(ctx, "agent", None)
        context = getattr(agent, "context", None)
        session = getattr(ctx, "session", None)
        if context is None or session is None:
            return "no session yet — ask something first"
        total = 0
        for message in session.to_dicts():
            total += len(str(message.get("content") or ""))
        used = total // 4
        limit = int(getattr(context, "max_tokens", 0) or 0) or 1
        pct = max(0, min(100, int(100 * used / limit)))
        width = 20
        filled = int(round(pct * width / 100))
        if pct < GREEN_LIMIT:
            color = "green"
        elif pct < YELLOW_LIMIT:
            color = "yellow"
        else:
            color = "red"
        bar = "\\u2588" * filled + "\\u2591" * (width - filled)
        return f"[{color}]{bar}[/] {pct}% (estimate — /token is exact)"

    api.command("ctxbar", "Context window as a colored fill bar", ctxbar,
                usage="/ctxbar")
'''

CTXBAR_MANIFEST = {
    "name": "ctxbar",
    "version": "1.0.0",
    "type": "plugin",
    "description": "Context window as a colored fill bar",
    "entry": "plugin.py",
}


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _agent(project):
    agent = Agent(config=BeeConfig(permissions={"mode": "auto"}),
                  workdir=str(project))
    ForgeTool.bind(agent)
    return agent


def _stage(project, name, files):
    root = project / ".beeagent" / "plugins" / name
    root.mkdir(parents=True, exist_ok=True)
    for filename, text in files.items():
        (root / filename).write_text(text, encoding="utf-8")
    return root


def _text(result) -> str:
    body = result.output
    if isinstance(body, str):
        return body
    buffer = io.StringIO()
    Console(file=buffer, width=200).print(body)
    return buffer.getvalue()


# --- guide --------------------------------------------------------------------

def test_guide_prints_the_contract_with_no_agent_needed():
    out = ForgeTool().execute(action="guide")
    assert out.error is False
    assert "setup(api)" in out.output and "SKILL.md" in out.output


# --- validate ---------------------------------------------------------------

def test_empty_folder_is_not_installable(project):
    root = project / ".beeagent" / "plugins" / "ctxbar"
    root.mkdir(parents=True)
    errors, _warnings = validate_staged(root)
    assert errors and "SKILL.md" in errors[0]


def test_broken_python_names_file_line_and_fix(project):
    root = _stage(project, "ctxbar", {"plugin.py": "def broken(:\n"})
    errors, _warnings = validate_staged(root)
    assert len(errors) == 1 and "plugin.py:1" in errors[0]


def test_plugin_without_contract_installs_nothing(project):
    root = _stage(project, "ctxbar", {"plugin.py": "X = 1\n"})
    errors, _warnings = validate_staged(root)
    assert any("TOOLS" in line and "setup" in line for line in errors)


def test_bad_name_never_reaches_the_loader(project):
    # A colon cannot survive as a folder name on Windows and splits streams
    # on NTFS — install_name_ok refuses it, and validation must say so
    # without touching the disk at all.
    root = project / ".beeagent" / "plugins" / "evil:pack"
    errors, _warnings = validate_staged(root)
    assert errors, "a hostile folder name must die in validation"
    assert not root.exists()


def test_risky_imports_warn_but_do_not_refuse(project):
    root = _stage(project, "ctxbar", {
        "plugin.py": "import subprocess\nTOOLS = []\n",
        "SKILL.md": "do things\n",
    })
    errors, warnings = validate_staged(root)
    assert not errors
    assert any("subprocess" in line for line in warnings)


# --- the loop -----------------------------------------------------------------

def test_forge_loop_installs_and_answers_without_restart(project):
    """Write → validate → install → /ctxbar, in one live agent, untrusted folder."""
    agent = _agent(project)
    _stage(project, "ctxbar", {
        "plugin.py": CTXBAR_PLUGIN,
        "plugin.json": json.dumps(CTXBAR_MANIFEST, ensure_ascii=False),
    })
    forge = ForgeTool()

    checked = forge.execute(action="validate", name="ctxbar")
    assert checked.error is False, checked.output

    installed = forge.execute(action="install", name="ctxbar")
    assert installed.error is False, installed.output
    assert "NEXT turn" in installed.output

    session = Session()
    session.add_user_message("x" * 4000)
    out = _text(dispatch(ReplContext(agent=agent, config=agent.config,
                                     session=session), "/ctxbar"))
    assert "\u2588" in out and "%" in out, out


def test_ctxbar_colors_follow_the_fill(project):
    """Green under half, yellow under 80, red at and past it — the exact
    indicator the feature was asked for, called directly (the dispatch path
    renders markup away, so colors are asserted on the raw string)."""
    import importlib.util
    from types import SimpleNamespace

    root = _stage(project, "ctxbar", {"plugin.py": CTXBAR_PLUGIN})
    spec = importlib.util.spec_from_file_location("ctxbar_demo", root / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    seen = {}
    module.setup(SimpleNamespace(
        command=lambda name, desc, handler, usage="": seen.setdefault("handler", handler)))

    def run(chars, limit=100):
        ctx = SimpleNamespace(
            agent=SimpleNamespace(context=SimpleNamespace(max_tokens=limit)),
            session=SimpleNamespace(
                to_dicts=lambda: [{"role": "user", "content": "x" * chars}]))
        return seen["handler"](ctx, [])

    assert run(40).startswith("[green]")
    assert run(240).startswith("[yellow]")
    assert run(400).startswith("[red]")
    assert run(400).endswith("(estimate — /token is exact)")


def test_forge_install_of_broken_code_changes_nothing(project):
    agent = _agent(project)
    _stage(project, "ctxbar", {"plugin.py": "def broken(:\n"})
    forge = ForgeTool()

    installed = forge.execute(action="install", name="ctxbar")
    assert installed.error is True
    assert "not installable" in installed.output
    assert "ctxbar" not in agent.plugins.manager.installed()
    assert "unknown command" in _text(dispatch(
        ReplContext(agent=agent, config=agent.config, session=Session()),
        "/ctxbar")).lower()


def test_forge_remove_takes_it_back_out(project):
    agent = _agent(project)
    _stage(project, "ctxbar", {"plugin.py": CTXBAR_PLUGIN})
    forge = ForgeTool()
    assert forge.execute(action="install", name="ctxbar").error is False

    removed = forge.execute(action="remove", name="ctxbar")
    assert removed.error is False
    assert "ctxbar" not in agent.plugins.manager.installed()
    assert "unknown command" in _text(dispatch(
        ReplContext(agent=agent, config=agent.config, session=Session()),
        "/ctxbar")).lower()


def test_forge_lists_only_what_it_forged(project):
    agent = _agent(project)
    forge = ForgeTool()
    assert "nothing forged" in forge.execute(action="list").output
    _stage(project, "ctxbar", {"SKILL.md": "draw bars\n"})
    assert forge.execute(action="install", name="ctxbar").error is False
    assert "ctxbar" in forge.execute(action="list").output


# --- the catalog and the market ----------------------------------------------
#
# The model can now download instead of authoring. Nothing below opens a
# connection: the market is a recording fake, and the couple of successful
# installs use templates that ship inside the package.

def _market(entries):
    from beeagent.plugins.catalog import Market

    return Market(entries=entries, policy=("MIT",), host="market.invalid")


def _skill_entry(name="demo-skill", text="do the demo\n"):
    import hashlib

    from beeagent.plugins.catalog import MarketItem

    raw = text.encode("utf-8")
    return MarketItem(
        name=name, type="skill", category="skills", description="a demo skill",
        source={"kind": "market", "id": f"demo--{name}"},
        id=f"demo--{name}", license="MIT", repo="demo/skills",
        path=f"skills/{name}", commit="a" * 40,
        download="https://market.invalid/SKILL.md",
        sha256=hashlib.sha256(raw).hexdigest(),
    )


def test_search_reads_the_shipped_catalog_and_the_market(project, monkeypatch):
    agent = _agent(project)
    from beeagent.plugins import catalog as catalog_module

    monkeypatch.setattr(catalog_module, "fetch_market",
                        lambda url, token="", timeout=0: _market([_skill_entry()]))
    out = ForgeTool().execute(action="search", query="code-review")

    assert out.error is False
    assert "code-review" in out.output and "catalog (shipped" in out.output
    assert "market (market.invalid" in out.output
    assert "forge action=download" in out.output


def test_search_survives_a_market_that_is_down(project, monkeypatch):
    agent = _agent(project)
    from beeagent.plugins import catalog as catalog_module

    def dead(url, token="", timeout=0):
        raise RuntimeError("503 cold start")

    monkeypatch.setattr(catalog_module, "fetch_market", dead)
    out = ForgeTool().execute(action="search", query="debugging")
    assert out.error is False
    assert "debugging" in out.output and "market: not reached" in out.output


def test_a_skill_downloads_and_answers_next_turn(project, monkeypatch):
    agent = _agent(project)
    forge = ForgeTool()
    out = forge.execute(action="download", target="code-review")

    assert out.error is False, out.output
    assert "installed" in out.output and "NEXT turn" in out.output
    assert agent.plugins.manager.is_installed("code-review")
    assert "code-review" in [s.name for s in agent.plugins.skills]
    assert "code-review" in forge.execute(action="installed").output


def test_a_plugin_download_needs_a_clear_yes(project):
    agent = _agent(project)
    forge = ForgeTool()
    refused = forge.execute(action="download", target="json-tool")

    assert refused.error is True
    assert "trust=true" in refused.output
    assert "json-tool" not in agent.plugins.manager.installed()

    installed = forge.execute(action="download", target="json-tool", trust=True)
    assert installed.error is False, installed.output
    assert agent.plugins.manager.is_installed("json-tool")


def test_a_market_skill_is_installed_only_after_its_hash_matches(project, monkeypatch):
    agent = _agent(project)
    from beeagent.plugins import catalog as catalog_module
    from beeagent.plugins import manager as manager_module

    entry = _skill_entry(text="do the demo\n")
    monkeypatch.setattr(catalog_module, "fetch_market",
                        lambda url, token="", timeout=0: _market([entry]))
    monkeypatch.setattr(manager_module, "fetch_bytes",
                        lambda url, timeout=0: b"do the demo\n")

    out = ForgeTool().execute(action="download", target=entry.id)
    assert out.error is False, out.output
    assert agent.plugins.manager.is_installed("demo-skill")
    assert "sha256" in out.output and "demo/skills" in out.output

    # The same entry with different bytes must leave the project untouched.
    bad = _skill_entry(name="tampered")
    monkeypatch.setattr(catalog_module, "fetch_market",
                        lambda url, token="", timeout=0: _market([bad]))
    monkeypatch.setattr(manager_module, "fetch_bytes",
                        lambda url, timeout=0: b"not what was promised\n")
    refused = ForgeTool().execute(action="download", target=bad.id)
    assert refused.error is True and "sha256" in refused.output
    assert not agent.plugins.manager.is_installed("tampered")


def test_a_market_mcp_entry_is_a_pointer_not_an_install(project, monkeypatch):
    agent = _agent(project)
    from beeagent.plugins.catalog import MarketItem
    from beeagent.plugins import catalog as catalog_module

    entry = MarketItem(
        name="demo-mcp", type="mcp", category="mcp", description="a demo server",
        source={"kind": "market", "id": "demo--mcp"}, id="demo--mcp",
        license="MIT", repo="demo/mcp", path="LICENSE", commit="b" * 40,
        download="https://market.invalid/LICENSE", sha256="0" * 64,
        run="npx -y demo-mcp",
    )
    monkeypatch.setattr(catalog_module, "fetch_market",
                        lambda url, token="", timeout=0: _market([entry]))
    out = ForgeTool().execute(action="download", target=entry.id)

    assert out.error is True
    assert "mcp_add" in out.output and "npx -y demo-mcp" in out.output
    assert "command=npx" in out.output and "-y" in out.output
    assert "demo-mcp" not in agent.plugins.manager.installed()


def test_adding_an_mcp_server_needs_trust_and_is_persisted(project):
    agent = _agent(project)
    forge = ForgeTool()
    refused = forge.execute(action="mcp_add", name="demo", command="npx",
                            args=["-y", "demo-mcp"])
    assert refused.error is True and "trust=true" in refused.output
    assert "demo" not in agent.plugins.manager.mcp_servers()

    added = forge.execute(action="mcp_add", name="demo", command="npx",
                          args=["-y", "demo-mcp"], trust=True)
    assert added.error is False, added.output
    assert agent.plugins.manager.mcp_servers()["demo"]["command"] == "npx"
    assert "-y" in agent.plugins.manager.mcp_servers()["demo"]["args"]
    assert "connect" in added.output


def test_mcp_add_accepts_one_command_string(project):
    agent = _agent(project)
    added = ForgeTool().execute(action="mcp_add", name="demo",
                                command="npx -y demo-mcp", trust=True)
    assert added.error is False, added.output
    server = agent.plugins.manager.mcp_servers()["demo"]
    assert server["command"] == "npx"
    assert server["args"] == ["-y", "demo-mcp"]


def test_connecting_an_mcp_server_reports_its_tools(project, monkeypatch):
    agent = _agent(project)
    agent.plugins.manager.add_mcp_server("demo", "npx", ["-y", "demo-mcp"])
    monkeypatch.setattr(agent.plugins, "connect_mcp",
                        lambda name, timeout=0: [{"name": "ping"},
                                                 {"name": "echo"}])
    out = ForgeTool().execute(action="connect", name="demo")

    assert out.error is False, out.output
    assert "2 tool(s)" in out.output and "ping" in out.output and "echo" in out.output


def test_market_fallback_never_sends_the_seat_to_another_host(project, monkeypatch):
    agent = _agent(project)
    agent.config.pool_url = "https://pool.invalid"
    agent.config.pool_token = "#beecode0000000001"
    seen = []
    from beeagent.plugins import catalog as catalog_module

    def fake(url, token="", timeout=0):
        seen.append((url, token))
        if url.startswith("https://pool.invalid"):
            raise RuntimeError("no market route here")
        return _market([_skill_entry()])

    monkeypatch.setattr(catalog_module, "fetch_market", fake)
    out = ForgeTool().execute(action="search", query="demo")
    assert out.error is False
    assert seen[0] == ("https://pool.invalid", "#beecode0000000001")
    assert seen[1][0] == "https://beecode-pool.onrender.com"
    assert seen[1][1] == "", "a seat token stays with the pool that issued it"
