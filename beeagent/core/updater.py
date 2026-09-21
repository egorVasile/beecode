"""Is there a newer BeeCode than the one that is running?

The question must never delay the first prompt, so nothing here is awaited at
startup: a worker thread asks GitHub for the published version, writes what it
learned into `.beeagent/update.json`, and the terminal reads that file. A slow
or dead network costs an update notice, not a boot.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path

# The version of the branch people install from. The contents API with a raw
# Accept header, rather than raw.githubusercontent.com: that CDN kept serving a
# version it had already replaced for minutes, and a stale answer is worse than
# no answer — it is the difference between "up to date" and silence.
CHECK_URL = "https://api.github.com/repos/egorVasile/beecode/contents/beeagent/__init__.py?ref=main"
CACHE_PATH = Path(".beeagent") / "update.json"
INTERVAL = 12 * 3600          # twice a day is enough to hear about a release
TIMEOUT = 4                   # seconds; a stalled network is not the user's problem
_VERSION_IN_SOURCE = re.compile(r'__version__\s*=\s*["\']([^"\']+)["\']')

_lock = threading.Lock()
_thread: threading.Thread | None = None


def parse_version(text: str) -> tuple:
    """`"0.9.10"` as a number you can compare, not a string that sorts wrongly."""
    parts = re.findall(r"\d+", text or "")[:3]
    return tuple(int(part) for part in parts) or (0,)


def is_newer(latest: str, current: str) -> bool:
    return parse_version(latest) > parse_version(current)


def _cache_file(workdir) -> Path:
    return Path(workdir) / CACHE_PATH


def read_cache(workdir=".") -> dict:
    try:
        body = json.loads(_cache_file(workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return body if isinstance(body, dict) else {}


def write_cache(workdir=".", **fields) -> dict:
    path = _cache_file(workdir)
    body = read_cache(workdir)
    body.update(fields)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(body), encoding="utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.replace(path)
    except OSError:
        return body
    return body


def fetch_latest(timeout: float = TIMEOUT) -> str:
    """The version published on the branch, or "" when it cannot be read."""
    request = urllib.request.Request(
        CHECK_URL, headers={"Accept": "application/vnd.github.raw",
                            "User-Agent": "beecode-update-check"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            text = response.read().decode("utf-8", "replace")
    except Exception:
        return ""
    found = _VERSION_IN_SOURCE.search(text)
    return found.group(1) if found else ""


def check(workdir=".", force: bool = False) -> dict:
    """Look once and remember. Returns the cache, which may say "too recent"."""
    cached = read_cache(workdir)
    if not force and time.time() - float(cached.get("checked_at", 0)) < INTERVAL:
        return cached
    latest = fetch_latest()
    if not latest:
        return write_cache(workdir, checked_at=time.time(), error="network")
    return write_cache(workdir, checked_at=time.time(), latest=latest, error="")


def start(workdir=".") -> None:
    """Run `check` in the background, once per process."""
    global _thread
    with _lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=check, kwargs={"workdir": workdir},
                                   name="beecode-update-check", daemon=True)
        _thread.start()


def wait(timeout: float = 1.0) -> bool:
    """Give the background check a moment; never longer than `timeout`."""
    thread = _thread
    if thread is not None and thread.is_alive():
        thread.join(timeout)
    return thread is None or not thread.is_alive()


def available(workdir=".", current: str = "") -> str:
    """The newer version to offer, or "" when there is nothing to say.

    A version the user already answered "no" to is not asked about twice — the
    next release will be.
    """
    if not current:
        from beeagent import __version__

        current = __version__
    cached = read_cache(workdir)
    latest = str(cached.get("latest") or "")
    if not latest or not is_newer(latest, current):
        return ""
    if str(cached.get("declined")) == latest:
        return ""
    return latest
