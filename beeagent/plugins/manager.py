"""Install/remove/list plugins, skills, and MCP servers.

Installed units live in `.beeagent/plugins/<name>/` (skills + plugin packs)
and `.beeagent/mcp.json` (MCP servers). State (installed, enabled) is tracked
in `.beeagent/plugins.json`.

A market install is the one path here that reaches the network, and it is written
around one rule: the bytes are hashed before they touch a disk, and a licence that
was not granted never gets that far.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from beeagent.plugins.catalog import (
    Catalog, CatalogItem, MarketError, MarketItem, TEMPLATES_DIR,
    fetch_bytes, install_name_ok, license_allowed, unpack,
)
from beeagent.i18n import L
from beeagent.tools.shell import decode

PLUGINS_DIR = Path(".beeagent") / "plugins"
STATE_PATH = Path(".beeagent") / "plugins.json"
MCP_CONFIG_PATH = Path(".beeagent") / "mcp.json"


def _as_market_item(item: CatalogItem) -> MarketItem:
    """Rebuild an index entry from an install record.

    A reinstall reads `.beeagent/plugins.json`, which stores the source as a plain
    dict; the gate and the hash live on the entry, so it goes back to being one.
    """
    src = item.source or {}
    return MarketItem(
        name=item.name, type=item.type, category=item.category,
        description=item.description, source=src,
        id=str(src.get("id") or item.name), license=str(src.get("license") or ""),
        repo=str(src.get("repo") or ""), path=str(src.get("path") or ""),
        commit=str(src.get("commit") or ""), download=str(src.get("download") or ""),
        sha256=str(src.get("sha256") or ""),
    )


def _artifact_name(entry: MarketItem) -> str:
    """What to call the download when it turns out to be one file, not an archive.

    A path naming a folder ("skills/demo") has no extension to keep, and the
    loader discovers a skill by its `SKILL.md`, so that is the name it gets.
    """
    tail = (entry.path or entry.download or "").replace("\\", "/").rstrip("/").split("/")[-1]
    return tail if "." in tail else "SKILL.md"


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

    def install(self, target: str, item: CatalogItem | None = None) -> dict:
        """Install by catalog name, git URL, or an already resolved market entry.

        `item` is how a caller that has just fetched the market index hands the
        entry over: resolving it is the command's job, verifying its bytes is
        this one.
        """
        item = item or self.catalog.get(target) or self.catalog.find_git(target)
        if item is None:
            raise ValueError(f"'{target}' not found in catalog (and not a git URL)")

        kind = item.source.get("kind")
        if kind == "builtin":
            self._install_builtin(item)
        elif kind == "mcp":
            self._install_mcp(item)
        elif kind == "git":
            self._install_git(item)
        elif kind == "market":
            self._install_market(item)
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
        # The keys of this report are pinned by tests: a caller that wants
        # provenance reads it off the item or the state it just wrote.
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

    def _install_market(self, item: CatalogItem) -> None:
        """Fetch one market entry and write it only if it is the promised bytes.

        The order is the whole point: licence, then hash, then disk. An entry
        whose licence nobody granted and a download whose sha256 does not match
        both fail before a single file exists, so a refused install leaves the
        project exactly as it was found.
        """
        entry = item if isinstance(item, MarketItem) else _as_market_item(item)

        if not install_name_ok(entry.name):
            raise MarketError(L(
                f"the index calls “{entry.name}” by a name that is not a folder name — "
                f"nothing was fetched or written",
                f"индекс называет «{entry.name}» так, что папкой это быть не может — "
                f"ничего не скачано и не записано"))
        if not license_allowed(entry.license):
            raise MarketError(L(
                f"“{entry.name}” is licensed “{entry.license or 'nothing — no licence field'}”, "
                f"which this client will not install. The licence, not the code, is what is "
                f"missing here",
                f"«{entry.name}» распространяется под лицензией "
                f"«{entry.license or 'ничего — поля лицензии нет'}», и такую этот клиент "
                f"не ставит: здесь не хватает лицензии, а не кода"))
        if not entry.sha256:
            raise MarketError(L(
                f"the index names no sha256 for “{entry.name}”, so its bytes cannot be "
                f"verified — nothing was downloaded or written",
                f"в индексе нет sha256 для «{entry.name}», а значит его байты не проверить — "
                f"ничего не скачано и не записано"))
        if not entry.download:
            raise MarketError(L(
                f"the index gives provenance for “{entry.name}” ({entry.provenance}) but no "
                f"download to fetch — it is a pointer, and there is nothing to install",
                f"индекс даёт для «{entry.name}» только происхождение ({entry.provenance}) "
                f"и не даёт адреса — это ссылка, ставить нечего"))

        raw = fetch_bytes(entry.download)
        digest = hashlib.sha256(raw).hexdigest()
        if digest != entry.sha256.lower():
            raise MarketError(L(
                f"sha256 mismatch for “{entry.name}”: the index promised {entry.sha256}, "
                f"the bytes at {entry.provenance} are {digest} — nothing was written",
                f"sha256 для «{entry.name}» не совпал: индекс обещал {entry.sha256}, "
                f"по адресу {entry.provenance} лежит {digest} — ничего не записано"))

        dst = self.plugins_dir / entry.name
        staging = self.plugins_dir / f"{entry.name}.market-partial"
        shutil.rmtree(staging, ignore_errors=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)
        try:
            unpack(raw, staging, fallback_name=_artifact_name(entry))
            if dst.exists():
                shutil.rmtree(dst)
            staging.replace(dst)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Remember where it came from, next to what was verified: `installed_at`
        # alone would not tell anyone which commit these bytes were hashed at.
        entry.source = dict(entry.source or {}) | entry.as_source()

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
