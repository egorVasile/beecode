"""The model extending itself: download an extension, or validate and switch on
one it just wrote.

Two loops share this tool. The catalog loop: `search` the shipped catalog and the
pool's licence-gated market, `download` a skill, plugin or MCP server, then
`connect` an MCP server so its tools answer. The authoring loop: draft a skill
(`SKILL.md`) or tool pack (`plugin.py`) with the `write` tool straight into
`.beeagent/plugins/<name>/`, check it with `forge action=validate`, repair with
`edit`, and activate it with `forge action=install` — no restart, no user-side
commands. What lands answers from the model's NEXT turn (the current request's
schemas already left).

Safety is the project's own stack, not a new one: writing goes through the
`write` tool (permission gate, path jail, undo journal), installing pins the
exact bytes for the trust gate (an edit re-arms the question, like
`/plugin install --trust`), market bytes are licence-gated and hash-checked
before they touch a disk, loading faces the same gate and error reporting as a
downloaded pack, and this tool itself is unsafe — granting it is a decision, in
`ask` mode a question per session. A skill is text and installs freely; a plugin
or MCP server is code that runs, so it needs `trust=true` after the user said
yes. See `beeagent/plugins/AUTHORING.md` for the file layouts the model reads.
"""
from __future__ import annotations

import os
import shlex

from .base import BaseTool, ToolResult

# The shipped pool also serves the licence-gated market. A self-hosted pool may
# carry `/v1/models` and `/v1/chat/completions` without the market route (ours
# does), so the shipped address is the fallback, not a second-class citizen.
DEFAULT_MARKET_POOL = "https://beecode-pool.onrender.com"
MARKET_POOL_ENV = "BEECODE_MARKET_URL"


def _run_command(run: str) -> tuple[str, list[str]] | None:
    """Split a market MCP entry's `run` string into a command and arguments.

    The index writes one executable per entry ("npx -y pkg@latest"), sometimes
    followed by hosted alternatives separated by "|". Only the first fragment is
    a command; a URL is a hosted endpoint, not something to spawn.
    """
    first = (run or "").split("|")[0].strip()
    if not first or "://" in first:
        return None
    try:
        parts = shlex.split(first, posix=True)
    except ValueError:
        parts = first.split()
    if not parts:
        return None
    return parts[0], parts[1:]


class ForgeTool(BaseTool):
    name = "forge"
    description = (
        "Download skills, plugins and MCP servers from the catalog or pool market, "
        "add an MCP server, or install a pack you wrote. Actions: guide, search, "
        "download, mcp_add, connect, validate, install, list, installed, remove. "
        "Installed units answer from the next turn."
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["guide", "search", "download", "mcp_add", "connect",
                         "validate", "install", "list", "installed", "remove"],
                "description": "What to do.",
            },
            "name": {
                "type": "string",
                "description": "Pack folder or MCP server name.",
            },
            "query": {
                "type": "string",
                "description": "Search words.",
            },
            "target": {
                "type": "string",
                "description": "Catalog name, market id or git URL.",
            },
            "trust": {
                "type": "boolean",
                "description": "Only after the user agreed: allow code installs.",
            },
            "connect": {
                "type": "boolean",
                "description": "Connect the MCP server now.",
            },
            "command": {
                "type": "string",
                "description": "mcp_add: executable (npx, uvx).",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "mcp_add: arguments.",
            },
        },
        "required": ["action"],
    }
    writes_files = True

    _agent = None

    @classmethod
    def bind(cls, agent) -> None:
        """The live agent, so install/remove can reload its extensions."""
        cls._agent = agent

    @classmethod
    def _manager(cls):
        agent = cls._agent
        if agent is None or getattr(agent, "plugins", None) is None:
            return None
        return agent.plugins.manager

    def execute(self, action: str = "validate", name: str = "",
                kind: str = "", query: str = "", target: str = "",
                trust: bool = False, connect: bool = False,
                command: str = "", args: list[str] | None = None) -> ToolResult:
        action = (action or "").strip().lower()
        if action == "guide":
            return self._guide()
        manager = self._manager()
        if manager is None:
            return ToolResult(
                output="forge: no live agent bound — install and reload from "
                       "the terminal instead: /plugin, then restart BeeCode.",
                error=True)
        if action == "search":
            return self._search(manager, query, kind)
        if action == "installed":
            return self._installed(manager)
        if action == "download":
            return self._download(manager, target or name, kind, trust, connect)
        if action == "mcp_add":
            return self._mcp_add(manager, name or target, command, args or [], trust)
        if action == "connect":
            return self._connect(name or target)
        if action == "list":
            return self._list(manager)
        name = (name or "").strip()
        if not name:
            return ToolResult(
                output="forge: name the pack — forge action=validate name=<folder> "
                       "under .beeagent/plugins/.",
                error=True)
        if action == "validate":
            return self._validate(manager, name)
        if action == "install":
            return self._install(manager, name, (kind or "").strip().lower())
        if action == "remove":
            return self._remove(manager, name)
        return ToolResult(
            output=f"forge: unknown action '{action}' — guide, search, download, "
                   f"mcp_add, connect, validate, install, list, installed, remove.",
            error=True)

    # --- the catalog and the market ------------------------------------------

    def _market(self, manager=None):
        """The pool's index, from the configured pool or the shipped fallback.

        Returns (market, url, failures). Raises MarketError when no host answers.
        The seat token travels only to the pool that issued it: the fallback is a
        different host, and a seat is not a password to paste into it.
        """
        from beeagent.plugins.catalog import MARKET_TIMEOUT, MarketError, fetch_market

        agent = type(self)._agent
        config = getattr(agent, "config", None)
        pool = str(getattr(config, "pool_url", "") or "").strip()
        token = str(getattr(config, "pool_token", "") or "").strip()
        candidates: list[tuple[str, str]] = []
        for url, sent in ((os.environ.get(MARKET_POOL_ENV, "").strip(), ""),
                          (pool, token),
                          (DEFAULT_MARKET_POOL, "")):
            if url and url.rstrip("/") not in [u.rstrip("/") for u, _ in candidates]:
                candidates.append((url, sent))
        failures = []
        for url, sent in candidates:
            try:
                market = fetch_market(url, sent, MARKET_TIMEOUT)
                return market, market.host or url, failures
            except Exception as e:
                failures.append(f"{url}: {e}")
        raise MarketError("; ".join(failures) or "no market address is configured")

    def _search(self, manager, query: str, kind: str = "") -> ToolResult:
        kind = (kind or "").strip().lower()
        query = (query or "").strip()
        words = query.lower()

        def fits(text: str) -> bool:
            return not words or words in str(text).lower()

        local = [i for i in manager.catalog.items()
                 if (not kind or i.type == kind)
                 and (fits(i.name) or fits(i.description) or fits(i.type))]
        lines: list[str] = []
        if local:
            lines.append(f"catalog (shipped, {len(local)} matching):")
            for item in local[:15]:
                lines.append(f"  [{item.type}] {item.name} — {item.description[:100]}")
            if len(local) > 15:
                lines.append(f"  … {len(local) - 15} more — narrow the query")
        try:
            market, host, _failures = self._market(manager)
        except Exception as e:
            market, host = None, ""
            lines.append(f"market: not reached — {e}")
        if market is not None:
            entries = [e for e in market.search(query) if not kind or e.type == kind]
            lines.append(f"market ({host}, {len(entries)} matching, "
                         f"{len(market.entries)} total):")
            for entry in entries[:20]:
                note = ""
                if entry.type == "mcp":
                    note = " — a pointer; add it with mcp_add using its run command"
                lines.append(
                    f"  [{entry.type}] {entry.name} — {entry.description[:90]} "
                    f"(licence {entry.license or '?'}, id {entry.id}){note}")
            if len(entries) > 20:
                lines.append(f"  … {len(entries) - 20} more — narrow the query")
        if not lines:
            return ToolResult(output=f"forge: nothing found for '{query}'.",
                              error=False)
        lines.append("download: forge action=download target=<name or id>; "
                     "plugins and MCP need trust=true after the user agrees.")
        return ToolResult(output="\n".join(lines), error=False)

    def _installed(self, manager) -> ToolResult:
        installed = manager.installed()
        if not installed:
            return ToolResult(output="forge: nothing installed yet — "
                                     "forge action=search finds what can be.",
                              error=False)
        lines = ["forge: installed extensions:"]
        for name, entry in sorted(installed.items()):
            if not isinstance(entry, dict):
                continue
            state = "enabled" if entry.get("enabled", True) else "disabled"
            source = (entry.get("source") or {}).get("kind", "?")
            lines.append(f"  [{entry.get('type', '?')}] {name} ({state}, {source}) — "
                         f"{str(entry.get('description', ''))[:100]}")
        return ToolResult(output="\n".join(lines), error=False)

    def _download(self, manager, target: str, kind: str = "",
                  trust: bool = False, connect: bool = False) -> ToolResult:
        from beeagent.plugins.catalog import MarketItem

        target = (target or "").strip()
        kind = (kind or "").strip().lower()
        if not target:
            return ToolResult(
                output="forge: name what to download — a catalog name, a market id, "
                       "or a git URL.", error=True)
        item = (manager.catalog.get(target)
                or next((i for i in manager.catalog.items()
                         if i.name.lower() == target.lower()), None)
                or manager.catalog.find_git(target))
        host = ""
        if item is None:
            try:
                market, host, _failures = self._market(manager)
            except Exception as e:
                return ToolResult(
                    output=f"forge: '{target}' is not in the shipped catalog, and the "
                           f"market was not reached — {e}", error=True)
            matches = market.find(target)
            if len(matches) > 1:
                return ToolResult(
                    output="forge: that name is not unique in the market — ask by id: "
                           + ", ".join(m.id for m in matches), error=True)
            item = matches[0] if matches else None
        if item is None:
            return ToolResult(
                output=f"forge: '{target}' is in neither the shipped catalog nor the "
                       f"market. Try action=search.", error=True)
        if kind and item.type != kind:
            return ToolResult(
                output=f"forge: '{target}' is a {item.type}, not a {kind}.", error=True)
        if item.type == "mcp" and item.source.get("kind") == "market":
            run = getattr(item, "run", "") or "the run command in the index"
            parsed = _run_command(run)
            call = (f"forge action=mcp_add name={item.name} command={parsed[0]} "
                    f"args={parsed[1]} trust=true" if parsed
                    else f"forge action=mcp_add name={item.name} command=... "
                         f"args=[...] trust=true")
            return ToolResult(
                output=f"forge: market MCP entries point at a licence, not an "
                       f"installable artefact. The index says: {run}. If the user "
                       f"agrees, add it with {call}.", error=True)
        if item.type != "skill" and not trust:
            return ToolResult(
                output=f"forge: '{target}' is a {item.type} — external code that runs "
                       f"inside BeeCode. Ask the user in plain text first; only then "
                       f"call again with trust=true. Nothing was installed.", error=True)
        try:
            report = manager.install(target, item=item)
        except Exception as e:
            return ToolResult(output=f"forge: could not install '{target}' — {e}",
                              error=True)
        agent = type(self)._agent
        try:
            load_errors = agent.reload_extensions()
        except (AttributeError, TypeError):
            load_errors = ["reload is not available here — restart BeeCode"]
        lines = [f"forge: ✅ {report['type']} {report['name']} installed from "
                 f"{host or 'the shipped catalog'} — it answers from your NEXT turn."]
        if isinstance(item, MarketItem):
            lines.append(f"  licence {item.license} — {item.provenance}")
            lines.append(f"  sha256 {item.sha256[:16]}… verified against the index")
        if report["type"] == "mcp":
            if connect:
                connected = self._connect(report["name"])
                lines.append(connected.output)
                if connected.error:
                    load_errors = list(load_errors) + [f"MCP connect failed: {connected.output}"]
            else:
                lines.append(f"  connect it: forge action=connect name={report['name']}")
        lines += [f"  ⚠ {line}" for line in load_errors]
        return ToolResult(output="\n".join(lines), error=bool(load_errors))

    def _mcp_add(self, manager, name: str, command: str,
                 args: list[str], trust: bool) -> ToolResult:
        from beeagent.plugins.catalog import install_name_ok

        name = (name or "").strip()
        command = (command or "").strip()
        if not name or not command:
            return ToolResult(
                output="forge: mcp_add needs name=<server> and command=<npx|uvx|...>.",
                error=True)
        if not install_name_ok(name):
            return ToolResult(output=f"forge: refusing MCP server name {name!r}.",
                              error=True)
        if not trust:
            return ToolResult(
                output=f"forge: adding MCP server '{name}' runs '{command}' on this "
                       f"machine. Ask the user in plain text first; only then call "
                       f"again with trust=true. Nothing was written.", error=True)
        args = [str(a) for a in (args or []) if str(a).strip()]
        if not args and " " in command:
            try:
                parts = shlex.split(command, posix=True)
            except ValueError:
                parts = command.split()
            if parts:
                command, args = parts[0], parts[1:]
        try:
            manager.add_mcp_server(name, command, args)
        except Exception as e:
            return ToolResult(output=f"forge: could not add MCP server '{name}' — {e}",
                              error=True)
        agent = type(self)._agent
        try:
            agent.reload_extensions()
        except (AttributeError, TypeError):
            pass
        return ToolResult(
            output=f"forge: ✅ MCP server {name} added: {command} {' '.join(args)}. "
                   f"Discover its tools with forge action=connect name={name}.",
            error=False)

    def _connect(self, name: str) -> ToolResult:
        import re

        name = (name or "").strip()
        if not name:
            return ToolResult(output="forge: connect needs name=<MCP server>.",
                              error=True)
        agent = type(self)._agent
        loader = getattr(agent, "plugins", None) if agent is not None else None
        manager = self._manager()
        if loader is None or manager is None:
            return ToolResult(output="forge: no live agent bound.", error=True)
        servers = manager.mcp_servers()
        if name not in servers:
            known = ", ".join(sorted(servers)) or "none"
            return ToolResult(
                output=f"forge: no MCP server '{name}'. Configured: {known}.",
                error=True)
        if not servers[name].get("enabled", True):
            return ToolResult(
                output=f"forge: MCP server '{name}' is disabled — /plugin enable "
                       f"{name} first.", error=True)
        prefix = "mcp_" + re.sub(r"\W+", "_", name).strip("_") + "_"
        if any(t.startswith(prefix) for t in loader.tool_names):
            return ToolResult(
                output=f"forge: MCP server '{name}' is already connected.",
                error=False)
        try:
            tools = loader.connect_mcp(name, timeout=90.0)
        except Exception as e:
            return ToolResult(
                output=f"forge: could not connect MCP server '{name}' — {e}",
                error=True)
        names = [t.get("name", "?") for t in tools]
        return ToolResult(
            output=f"forge: ✅ MCP {name} connected — {len(names)} tool(s): "
                   + (", ".join(names[:20]) or "none advertised"), error=False)

    def _guide(self) -> ToolResult:
        """The authoring contract, from beside this file — no path for the
        model to guess, no prompt space spent until asked."""
        from pathlib import Path

        try:
            text = (Path(__file__).resolve().parent.parent
                    / "plugins" / "AUTHORING.md").read_text(encoding="utf-8")
        except (OSError, ValueError):
            return ToolResult(output="forge: authoring doc is missing next "
                                     "to the install — write SKILL.md and/or "
                                     "plugin.py with TOOLS/setup(api) into "
                                     ".beeagent/plugins/<name>/ instead.",
                              error=True)
        return ToolResult(output=text, error=False)

    def is_safe(self) -> bool:
        # Installing runs code: the grant gate asks in `ask` mode, and one
        # `/allow forge` covers the session. Reading (validate/list) rides
        # along — a validator that runs unasked is how repair loops stall.
        return False

    # --- actions -----------------------------------------------------------

    def _dir(self, manager, name: str):
        from pathlib import Path

        return Path(manager.plugins_dir) / name

    def _validate(self, manager, name: str) -> ToolResult:
        from beeagent.plugins.validate import validate_staged

        errors, warnings = validate_staged(self._dir(manager, name))
        if errors:
            lines = [f"forge: {name} is not installable yet — fix and validate again:"]
            lines += [f"  ✖ {line}" for line in errors]
            lines += [f"  ⚠ {line}" for line in warnings]
            return ToolResult(output="\n".join(lines), error=True)
        lines = [f"forge: {name} validates clean — install it with "
                 f"forge action=install name={name}."]
        lines += [f"  ⚠ {line}" for line in warnings]
        return ToolResult(output="\n".join(lines), error=False)

    def _infer_kind(self, manager, name: str) -> str:
        return "plugin" if (self._dir(manager, name) / "plugin.py").is_file() \
            else "skill"

    def _install(self, manager, name: str, kind: str) -> ToolResult:
        from beeagent.plugins.validate import validate_staged

        errors, warnings = validate_staged(self._dir(manager, name))
        if errors:
            lines = [f"forge: {name} is not installable yet — fix and validate again:"]
            lines += [f"  ✖ {line}" for line in errors]
            return ToolResult(output="\n".join(lines), error=True)
        kind = kind or self._infer_kind(manager, name)
        if kind not in ("skill", "plugin"):
            return ToolResult(
                output=f"forge: kind is '{kind}' — 'skill' or 'plugin'.",
                error=True)
        try:
            report = manager.install_local(name, kind,
                                           description=self._describe(manager, name))
        except (ValueError, OSError) as e:
            return ToolResult(output=f"forge: install refused — {e}", error=True)
        agent = type(self)._agent
        try:
            load_errors = agent.reload_extensions()
        except (AttributeError, TypeError):
            load_errors = ["reload is not available here — restart BeeCode"]
        contributions = []
        try:
            registry = agent.plugins.extensions
            contributions = [f"{c.kind} {c.name}"
                             for c in registry.by_plugin(name)]
        except AttributeError:
            pass
        lines = [f"forge: ✅ {report['type']} {name} installed "
                 f"({', '.join(report['markers'])}) — it answers from your "
                 f"NEXT turn, not this one."]
        if contributions:
            lines.append("  landed: " + ", ".join(contributions))
        else:
            lines.append("  landed: nothing new registered — if it should have, "
                         "read the loader errors below")
        lines += [f"  ⚠ {line}" for line in warnings]
        if load_errors:
            lines.append("  loader errors (repair, validate, install again):")
            lines += [f"  ✖ {line}" for line in load_errors]
            return ToolResult(output="\n".join(lines), error=True)
        return ToolResult(output="\n".join(lines), error=False)

    def _describe(self, manager, name: str) -> str:
        import json

        root = self._dir(manager, name)
        try:
            manifest = json.loads((root / "plugin.json").read_text(encoding="utf-8"))
            if isinstance(manifest, dict) and manifest.get("description"):
                return str(manifest["description"])[:120]
        except (OSError, ValueError):
            pass
        try:
            head = (root / "SKILL.md").read_text(encoding="utf-8").splitlines()
            for line in head[:20]:
                if line.strip():
                    return line.strip("# ").strip()[:120]
        except (OSError, ValueError):
            pass
        return f"forged pack {name}"

    def _remove(self, manager, name: str) -> ToolResult:
        try:
            manager.uninstall(name)
        except (ValueError, OSError) as e:
            return ToolResult(output=f"forge: cannot remove '{name}' — {e}",
                              error=True)
        agent = type(self)._agent
        try:
            agent.reload_extensions()
        except (AttributeError, TypeError):
            pass
        return ToolResult(output=f"forge: 🗑 {name} removed — restart-free, "
                                 f"like the install was.",
                          error=False)

    def _list(self, manager) -> ToolResult:
        try:
            installed = manager.installed()
        except (OSError, ValueError):
            installed = {}
        forged = {name: entry for name, entry in installed.items()
                  if isinstance(entry, dict)
                  and entry.get("source", {}).get("kind") == "local"}
        if not forged:
            return ToolResult(output="forge: nothing forged yet — write a pack "
                                     "into .beeagent/plugins/<name>/ first.",
                              error=False)
        lines = ["forge: forged packs:"]
        for name, entry in sorted(forged.items()):
            state = "enabled" if entry.get("enabled", True) else "disabled"
            lines.append(f"  {entry.get('type', '?')} {name} ({state}) — "
                         f"{entry.get('description', '')}"[:120])
        return ToolResult(output="\n".join(lines), error=False)
