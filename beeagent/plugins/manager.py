"""Install/remove/list plugins, skills, and MCP servers.

Installed units live in `.beeagent/plugins/<name>/` (skills + plugin packs)
and `.beeagent/mcp.json` (MCP servers). State (installed, enabled) is tracked
in `.beeagent/plugins.json`.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from beeagent.plugins.catalog import Catalog, CatalogItem, TEMPLATES_DIR
from beeagent.tools.shell import decode

PLUGINS_DIR = Path(".beeagent") / "plugins"
STATE_PATH = Path(".beeagent") / "plugins.json"
MCP_CONFIG_PATH = Path(".beeagent") / "mcp.json"


class PluginManager:
    def __init__(self, catalog: Catalog | None = None,
                 plugins_dir: Path = PLUGINS_DIR,
                 state_path: Path = STATE_PATH,
                 mcp_path: Path = MCP_CONFIG_PATH):
        self.catalog = catalog or Catalog()
        self.plugins_dir = plugins_dir
        self.state_path = state_path
        self.mcp_path = mcp_path

    # --- state ---------------------------------------------------------------

    def _state(self) -> dict:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"installed": {}}

    def _save_state(self, state: dict) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def installed(self) -> dict[str, dict]:
        return self._state().get("installed", {})

    def is_installed(self, name: str) -> bool:
        return name in self.installed()

    def is_enabled(self, name: str) -> bool:
        return self.installed().get(name, {}).get("enabled", True)

    def set_enabled(self, name: str, enabled: bool) -> None:
        state = self._state()
        if name in state["installed"]:
            state["installed"][name]["enabled"] = enabled
            self._save_state(state)

    # --- install --------------------------------------------------------------

    def install(self, target: str) -> dict:
        """Install by catalog name or git URL. Returns an install report."""
        item = self.catalog.get(target) or self.catalog.find_git(target)
        if item is None:
            raise ValueError(f"'{target}' not found in catalog (and not a git URL)")

        kind = item.source.get("kind")
        if kind == "builtin":
            self._install_builtin(item)
        elif kind == "mcp":
            self._install_mcp(item)
        elif kind == "git":
            self._install_git(item)
        else:
            raise ValueError(f"Unknown source kind '{kind}' for '{item.name}'")

        state = self._state()
        state.setdefault("installed", {})[item.name] = {
            "type": item.type,
            "category": item.category,
            "description": item.description,
            "enabled": True,
            "installed_at": datetime.now().isoformat(),
            "source": item.source,
        }
        self._save_state(state)
        return {"name": item.name, "type": item.type, "description": item.description}

    def _install_builtin(self, item: CatalogItem) -> None:
        src = TEMPLATES_DIR / item.source["path"]
        if not src.is_dir():
            raise ValueError(f"builtin template missing: {src}")
        dst = self.plugins_dir / item.name
        if dst.exists():
            shutil.rmtree(dst)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst)

    def _install_mcp(self, item: CatalogItem) -> None:
        cfg = self._mcp_config()
        cfg.setdefault("servers", {})[item.name] = {
            "command": item.source["command"],
            "args": list(item.source.get("args", [])),
            "env": item.source.get("env", {}),
            "enabled": True,
        }
        self._save_mcp_config(cfg)

    def _install_git(self, item: CatalogItem) -> None:
        url = item.source["url"]
        dst = self.plugins_dir / item.name
        if dst.exists():
            shutil.rmtree(dst)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(
            ["git", "clone", "--depth", "1", url, str(dst)],
            capture_output=True, timeout=180,
        )
        if res.returncode != 0:
            if dst.exists():
                shutil.rmtree(dst, ignore_errors=True)
            err = decode(res.stderr)
            raise RuntimeError(f"git clone failed: {err.strip()[:300]}")

    # --- uninstall ------------------------------------------------------------

    def uninstall(self, name: str) -> dict:
        state = self._state()
        entry = state.get("installed", {}).pop(name, None)
        if entry is None:
            raise ValueError(f"'{name}' is not installed")

        if entry.get("type") == "mcp":
            cfg = self._mcp_config()
            cfg.get("servers", {}).pop(name, None)
            self._save_mcp_config(cfg)
        else:
            dst = self.plugins_dir / name
            if dst.exists():
                shutil.rmtree(dst)
        self._save_state(state)
        return {"name": name, "removed": True}

    # --- discovery ------------------------------------------------------------

    def _find_marker(self, name: str, marker: str) -> list[Path]:
        """Dirs holding `marker`, up to two folders below the install root.

        Builtin templates keep SKILL.md/plugin.py at the root, while cloned
        repos nest them (skills/<name>/SKILL.md).
        """
        root = self.plugins_dir / name
        if not root.is_dir():
            return []
        if (root / marker).is_file():
            return [root]
        for depth in (1, 2):
            pattern = "/".join(["*"] * depth) + "/" + marker
            found = sorted({p.parent for p in root.glob(pattern)})
            if found:
                return found
        return []

    def installed_skill_dirs(self) -> list[Path]:
        """Enabled skill/plugin dirs that contain a SKILL.md."""
        out = []
        for name, entry in self.installed().items():
            if not entry.get("enabled", True) or entry.get("type") == "mcp":
                continue
            out.extend(self._find_marker(name, "SKILL.md"))
        return out

    def installed_plugin_dirs(self) -> list[Path]:
        """Enabled plugin dirs with an entry-point plugin.py."""
        out = []
        for name, entry in self.installed().items():
            if not entry.get("enabled", True) or entry.get("type") == "mcp":
                continue
            out.extend(self._find_marker(name, "plugin.py"))
        return out

    # --- mcp config ------------------------------------------------------------

    def _mcp_config(self) -> dict:
        try:
            return json.loads(self.mcp_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"servers": {}}

    def _save_mcp_config(self, cfg: dict) -> None:
        self.mcp_path.parent.mkdir(parents=True, exist_ok=True)
        self.mcp_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

    def mcp_servers(self) -> dict[str, dict]:
        servers = self._mcp_config().get("servers", {})
        # merge installed-via-catalog servers (source of truth for enable state)
        for name, entry in self.installed().items():
            if entry.get("type") == "mcp":
                src = entry.get("source", {})
                merged = servers.get(name, {
                    "command": src.get("command", ""),
                    "args": list(src.get("args", [])),
                    "env": src.get("env", {}),
                })
                merged["enabled"] = entry.get("enabled", True)
                servers[name] = merged
        return servers

    def add_mcp_server(self, name: str, command: str, args: list[str] | None = None,
                       env: dict | None = None) -> None:
        cfg = self._mcp_config()
        cfg.setdefault("servers", {})[name] = {
            "command": command,
            "args": list(args or []),
            "env": env or {},
            "enabled": True,
        }
        self._save_mcp_config(cfg)

        state = self._state()
        state.setdefault("installed", {})[name] = {
            "type": "mcp",
            "category": "mcp",
            "description": f"MCP server {command} {' '.join(args or [])}".strip(),
            "enabled": True,
            "installed_at": datetime.now().isoformat(),
            "source": {"kind": "mcp", "command": command, "args": list(args or [])},
        }
        self._save_state(state)
