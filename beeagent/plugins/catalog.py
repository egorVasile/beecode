"""Catalog of installable skills, plugins, and MCP servers.

Bundled JSON (`data/catalog.json`) plus optional remote overlay. Each item is
either a builtin template (files shipped inside the package), an MCP server
definition (command + args), or a git URL to clone from.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
CATALOG_PATH = DATA_DIR / "catalog.json"
TEMPLATES_DIR = Path(__file__).parent / "templates"

TYPE_ICON = {"skill": "📚", "plugin": "🧩", "mcp": "🔌"}


@dataclass
class CatalogItem:
    name: str
    type: str          # "skill" | "plugin" | "mcp"
    category: str
    description: str
    source: dict       # {"kind": "builtin"|"mcp"|"git", ...}


class Catalog:
    def __init__(self, path: Path = CATALOG_PATH):
        self.path = path
        self._items: dict[str, CatalogItem] | None = None

    def _load(self) -> dict[str, CatalogItem]:
        if self._items is not None:
            return self._items
        items: dict[str, CatalogItem] = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data.get("items", []):
                item = CatalogItem(
                    name=raw["name"],
                    type=raw.get("type", "plugin"),
                    category=raw.get("category", raw.get("type", "plugin")),
                    description=raw.get("description", ""),
                    source=raw.get("source", {}),
                )
                items[item.name] = item
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        self._items = items
        return items

    def items(self, category: str | None = None) -> list[CatalogItem]:
        all_items = list(self._load().values())
        if category is None:
            return all_items
        return [i for i in all_items if i.category == category or i.type == category]

    def get(self, name: str) -> CatalogItem | None:
        return self._load().get(name)

    def find_git(self, url: str) -> CatalogItem | None:
        """Allow installing anything by git URL even if not in the catalog.

        Only real URLs count, so a typo'd catalog name fails fast instead of
        being cloned from somewhere.
        """
        if not ("://" in url or url.startswith("git@") or url.endswith(".git")):
            return None
        name = url.rstrip("/").split("/")[-1].removesuffix(".git")
        if not name or name in ("//", ":"):
            return None
        return CatalogItem(
            name=name,
            type="plugin",
            category="plugins",
            description=f"installed from {url}",
            source={"kind": "git", "url": url},
        )

    def search(self, query: str) -> list[CatalogItem]:
        q = query.lower()
        return [
            i for i in self._load().values()
            if q in i.name.lower() or q in i.description.lower() or q in i.type.lower()
        ]
