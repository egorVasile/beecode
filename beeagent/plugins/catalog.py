"""Catalog of installable skills, plugins, and MCP servers.

Bundled JSON (`data/catalog.json`) plus the remote market the pool answers on
`/v1/market`. Each item is either a builtin template (files shipped inside the
package), an MCP server definition (command + args), a git URL to clone from, or
a market entry: a licence-gated pointer to a pinned commit somewhere public.

The remote half is fetched only when a command asks for it -- nothing here opens a
connection at import, because a marketplace must never be able to delay a boot.
"""
from __future__ import annotations

import io
import json
import re
import tarfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from beeagent.i18n import L
from beeagent.utils.sanitize import strip_terminal

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


# --- the remote market ----------------------------------------------------------
#
# The pool answers `GET /v1/market` with an index of licence-clean skills, each one
# a pointer: repo, path, pinned commit, licence, and the sha256 of the bytes at
# that commit. It is an index and not a mirror -- the bytes come from the public
# source they were found in, so nothing is redistributed that nobody granted, and
# the pool box cannot become the thing that has to be trusted.
#
# Everything below is reached from a command, never from an import.

MARKET_PATH = "/v1/market"

# One static JSON file on a box that does no work to answer it. The only honest
# reason to wait is a free instance that has to wake, and 12 s is what `/seat`
# already allows; longer belongs to a boot incident, not to a market listing.
MARKET_TIMEOUT = 12.0

# An artifact is a skill folder, not a dataset. Refusing an oversized body is also
# the difference between holding it in memory and filling a disk with it.
MAX_ARTIFACT = 8 * 1024 * 1024

# What this client will install whatever the index claims, in SPDX spelling.
# The pool filters these out server-side and they stay filtered here: a stale
# mirror of `index.json` -- or a hand-edited one -- must not be able to smuggle an
# excluded skill back in, so the allow list lives on the client and the index can
# only ever narrow it, never widen it. A licence that is not on this list, or is
# missing altogether, is refused: "no licence stated" is not "no licence needed".
OPEN_LICENSES = frozenset({
    "mit", "apache-2.0", "bsd-2-clause", "bsd-3-clause", "isc", "zlib",
    "unlicense", "cc0-1.0", "cc-by-4.0", "cc-by-3.0", "mpl-2.0", "upl-1.0",
    "epl-2.0", "epl-1.0", "0bsd", "python-2.0", "psf-2.0", "ofl-1.1", "wtfpl",
})

# Spellings a human writes when they do not know SPDX ids exist.
_LICENSE_ALIAS = {
    "apache": "apache-2.0", "apache2": "apache-2.0", "apache-2": "apache-2.0",
    "bsd": "bsd-3-clause", "bsd3": "bsd-3-clause", "bsd2": "bsd-2-clause",
    "public-domain": "unlicense", "cc-by": "cc-by-4.0", "cc0": "cc0-1.0",
    "mpl": "mpl-2.0", "python": "python-2.0",
}

# The index groups its entries by kind; the local catalog uses the same words.
KIND_FOR_GROUP = {
    "skill": "skill", "skills": "skill",
    "plugin": "plugin", "plugins": "plugin",
    "mcp": "mcp", "mcps": "mcp", "server": "mcp", "servers": "mcp",
    "mcp-server": "mcp", "mcp_servers": "mcp", "": "skill",
}


class MarketError(Exception):
    """A market that could not be reached, read, or trusted. The text is for the
    user as it stands, so the caller only has to print it."""


def _httpx():
    """httpx, imported when the market is asked for and never at boot.

    It costs half a second to load, and `beeagent.plugins` is in the import chain
    of every start -- a marketplace is not worth putting on that path. Tests
    replace this one function and nothing reaches a socket.
    """
    import httpx
    return httpx


def _clean(value: object, limit: int = 300) -> str:
    """Remote text is third-party text: no escapes, no control characters, and no
    embedded newline that would tear a table row in half."""
    text = re.sub(r"\s+", " ", str(value if value is not None else "")).strip()
    return strip_terminal(text)[:limit]


def license_key(value: object) -> str:
    """A licence name in the one spelling the gate compares against."""
    text = re.sub(r"[\s_]+", "-", str(value or "").strip().lower())
    text = re.sub(r"[-.]{2,}", "-", text).strip("-.")
    if not text:
        return ""
    for _ in range(2):                     # "The MIT License" -> "mit"
        shorter = re.sub(r"-(licen[cs]e)$", "", text)
        shorter = re.sub(r"^the-", "", shorter)
        if shorter == text:
            break
        text = shorter
    return _LICENSE_ALIAS.get(text, text)


def license_allowed(value: object, policy=()) -> bool:
    """The client-side half of the licence gate.

    `policy` is what the index itself declares it filtered by; it can only narrow
    the client's list, because the number of ways an index can be wrong is the
    number of ways someone else's licence is our problem.
    """
    key = license_key(value)
    if not key or key not in OPEN_LICENSES:
        return False
    named = {k for k in (license_key(p) for p in (policy or ())) if k}
    return not named or key in named


@dataclass
class MarketItem(CatalogItem):
    """One index entry: provenance, and the hash of the bytes it points at."""

    id: str = ""
    license: str = ""
    repo: str = ""
    path: str = ""
    commit: str = ""
    download: str = ""
    sha256: str = ""

    @property
    def provenance(self) -> str:
        """Where this came from, in one line: provenance over bytes."""
        where = self.repo or self.download
        if self.path:
            where = f"{where}/{self.path}" if where else self.path
        if self.commit:
            where = f"{where}@{self.commit[:12]}" if where else self.commit[:12]
        return where or L("unknown source", "неизвестный источник")

    def as_source(self) -> dict:
        """The install record: enough to audit the entry years later."""
        return {"kind": "market", "id": self.id, "repo": self.repo, "path": self.path,
                "commit": self.commit, "license": self.license, "download": self.download,
                "sha256": self.sha256}


@dataclass
class Market:
    """A fetched index, already passed through the licence gate."""

    entries: list[MarketItem] = field(default_factory=list)
    policy: tuple[str, ...] = ()
    generated: str = ""
    excluded: int = 0
    published_excluded: int = 0
    host: str = ""

    def search(self, query: str) -> list[MarketItem]:
        q = (query or "").lower()
        if not q:
            return list(self.entries)
        return [e for e in self.entries
                if q in e.name.lower() or q in e.id.lower()
                or q in e.description.lower() or q in e.type.lower()]

    def find(self, target: str) -> list[MarketItem]:
        """Every entry answering to this name or id -- the caller decides what a
        crowd means, because guessing between two publishers installs the wrong
        thing quietly."""
        wanted = (target or "").strip().lower()
        exact = [e for e in self.entries if e.id.lower() == wanted]
        if exact:
            return exact
        return [e for e in self.entries if e.name.lower() == wanted]


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def parse_market(body: object, host: str = "") -> Market:
    """Turn the index into entries, refusing what carries no granted licence."""
    if not isinstance(body, dict):
        raise MarketError(L("the market answered with something that is not an index",
                            "рынок ответил чем-то, что индексом не является"))
    policy_node = body.get("policy") if isinstance(body.get("policy"), dict) else {}
    raw_policy = policy_node.get("allowed_licenses") or policy_node.get("allowed_licence")
    policy = tuple(_clean(p, 60) for p in raw_policy if _clean(p, 60)) if isinstance(
        raw_policy, (list, tuple)) else ()
    counts = body.get("counts") if isinstance(body.get("counts"), dict) else {}

    groups: list[tuple[str, object]]
    raw_items = body.get("items")
    if isinstance(raw_items, dict):
        # The shape the pool serves: {"skills": [...], "plugins": [...], "mcp": [...]}.
        groups = [(str(kind), rows) for kind, rows in raw_items.items()]
    elif isinstance(raw_items, list):
        groups = [("", raw_items)]
    else:
        raise MarketError(L("the market index has no items in it",
                            "в индексе рынка нет ни одной записи"))

    entries: list[MarketItem] = []
    excluded = 0
    for group, rows in groups:
        for raw in (rows if isinstance(rows, list) else []):
            if not isinstance(raw, dict):
                continue
            licence = _clean(raw.get("license") or raw.get("licence"), 60)
            if not license_allowed(licence, policy):
                excluded += 1
                continue
            source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
            name = _clean(raw.get("name")) or _clean(raw.get("id")) or "unnamed"
            entries.append(MarketItem(
                name=name,
                # The group wins: it is what the publisher filed the entry under.
                # `kind` is the field the live index carries, `type` the one the
                # local catalog uses, and an entry may sit in an unknown group.
                type=KIND_FOR_GROUP.get(str(group).strip().lower(),
                                        _clean(raw.get("kind") or raw.get("type"), 20)
                                        or "skill"),
                category=str(group).strip().lower() or "skills",
                description=_clean(raw.get("description")),
                source={"kind": "market", "id": _clean(raw.get("id"), 120) or name},
                id=_clean(raw.get("id"), 120) or name,
                license=licence,
                repo=_clean(source.get("repo"), 200),
                path=_clean(source.get("path"), 300),
                commit=_clean(source.get("commit"), 80),
                download=_clean(raw.get("download") or source.get("download"), 500),
                sha256=_clean(raw.get("sha256"), 80).lower(),
            ))
    return Market(entries=entries, policy=policy,
                  generated=_clean(body.get("generated"), 40), excluded=excluded,
                  published_excluded=_int(counts.get("excluded")), host=host)


def market_headers(token: str) -> dict:
    """The seat token, sent to the pool that issued it and to nowhere else.

    A listing is public on the pool, so an install with no seat still sees one;
    the header is there because the pool knows who is asking.
    """
    from beeagent import __version__

    headers = {"User-Agent": f"beecode/{__version__}"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_market(url: str, token: str = "", timeout: float = MARKET_TIMEOUT) -> Market:
    """Ask the pool for its index. Raises `MarketError`, which is always readable.

    This is the only market call that touches the network, and only a command
    calls it: nothing here runs at import or at startup.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise MarketError(L("the market needs a pool address — /pool url https://…",
                            "рынку нужен адрес пула — /pool url https://…"))
    host = urlsplit(url).hostname or url
    endpoint = url.rstrip("/") + MARKET_PATH
    httpx = _httpx()
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(endpoint, headers=market_headers(token))
    except Exception as e:
        raise MarketError(L(
            f"the market at {host} was not reached ({e.__class__.__name__}) — a free "
            f"instance sleeps when nobody uses it, so ask again in a moment",
            f"рынок на {host} не ответил ({e.__class__.__name__}) — бесплатный инстанс "
            f"спит без обращений, спросишь через минуту")) from e
    if response.status_code != 200:
        reason = ""
        try:
            reason = str((response.json() or {}).get("error") or "").strip()
        except Exception:
            reason = ""
        raise MarketError(L(
            f"the market at {host} answered {response.status_code}"
            + (f" — {reason}" if reason else ""),
            f"рынок на {host} ответил {response.status_code}"
            + (f" — {reason}" if reason else "")))
    try:
        body = response.json()
    except Exception as e:
        raise MarketError(L("the market index is not JSON — that is the pool's problem, "
                            "not this install's",
                            "индекс рынка — не JSON: это поломка пула, а не этой сборки"),
                          ) from e
    return parse_market(body, host=host)


def fetch_bytes(url: str, timeout: float = MARKET_TIMEOUT) -> bytes:
    """The bytes behind an index entry, from the public source it names.

    No seat token here: the artifact is public by licence, and a credential that
    leaves for someone else's host is a leaked credential.
    """
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise MarketError(L(f"the index names no download to fetch ({url or 'empty'})",
                            f"в индексе нет адреса для скачивания ({url or 'пусто'})"))
    httpx = _httpx()
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            response = client.get(url, headers=market_headers(""))
    except Exception as e:
        raise MarketError(L(
            f"the download at {urlsplit(url).hostname or url} did not answer "
            f"({e.__class__.__name__})",
            f"скачивание с {urlsplit(url).hostname or url} не ответило "
            f"({e.__class__.__name__})")) from e
    if response.status_code != 200:
        raise MarketError(L(f"the download answered {response.status_code}",
                            f"скачивание ответило {response.status_code}"))
    raw = bytes(response.content or b"")
    if not raw:
        raise MarketError(L("the download came back empty", "скачивание вернулось пустым"))
    if len(raw) > MAX_ARTIFACT:
        raise MarketError(L(f"the download is {len(raw)} bytes, over the "
                            f"{MAX_ARTIFACT // (1024 * 1024)} MiB a skill should need",
                            f"скачивание весит {len(raw)} байт — больше, чем {MAX_ARTIFACT // (1024 * 1024)} MiB, "
                            f"которые скилу нужны"))
    return raw


def _member_name(name: str) -> str:
    """A path from an archive, checked before it reaches a disk.

    Zip and tar members are strings a stranger chose: absolute paths and `..` are
    how an archive escapes the folder it is unpacked into.
    """
    text = (name or "").replace("\\", "/")
    if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise MarketError(L(f"an archived path is absolute: {name!r}",
                            f"путь в архиве абсолютный: {name!r}"))
    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise MarketError(L(f"an archived path escapes the install folder: {name!r}",
                            f"путь в архиве выходит из папки установки: {name!r}"))
    return "/".join(parts)


# What a folder may be called on the machines BeeCode runs on.
_BAD_FOLDER = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_DEVICE = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)


def install_name_ok(name: str) -> bool:
    """Is this index entry's name usable as the folder it installs into?

    The name comes from someone else's repository, and it is the one place a
    market entry reaches the filesystem outside `.beeagent/plugins/` -- so a
    separator, a dot-dot, or a Windows device name is a refusal, not a surprise.
    """
    text = (name or "").strip()
    if not text or len(text) > 60 or text in (".", ".."):
        return False
    if text != Path(text).name or _BAD_FOLDER.search(text) or _WINDOWS_DEVICE.match(text):
        return False
    return True


def _is_tar(raw: bytes) -> bool:
    """The ustar magic sits at byte 257 of a tar header, in both plain and gzip form."""
    return len(raw) > 512 and raw[257:262] == b"ustar"


def unpack(raw: bytes, dst: Path, fallback_name: str = "SKILL.md") -> None:
    """Write verified bytes into `dst`: an archive is unpacked, anything else is
    kept as the one file it is. `dst` must not exist yet.

    The hash above this says the download is what the index promised; it says
    nothing about how loud it expands, so the total written is capped too.
    """
    written = 0

    def _sink(handle, target: Path) -> None:
        """Copy one member out, in bounded chunks, counting everything written."""
        nonlocal written
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "wb") as out:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > MAX_ARTIFACT:
                    raise MarketError(L(
                        f"the download unpacks to more than "
                        f"{MAX_ARTIFACT // (1024 * 1024)} MiB — it stopped there",
                        f"скачивание разворачивается больше чем в "
                        f"{MAX_ARTIFACT // (1024 * 1024)} MiB — на этом остановились"))
                out.write(chunk)

    head = raw[:2]
    if head == b"PK":
        dst.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                _sink(archive.open(info), dst / _member_name(info.filename))
        return
    if head == b"\x1f\x8b" or _is_tar(raw):
        dst.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(raw), mode="r:*") as archive:
            for member in archive.getmembers():
                if not member.isfile():
                    continue
                handle = archive.extractfile(member)
                if handle is None:
                    continue
                _sink(handle, dst / _member_name(member.name))
        return
    # Not an archive: one file, named for what the index says it is.
    _sink(io.BytesIO(raw), dst / _member_name(fallback_name or "SKILL.md"))
