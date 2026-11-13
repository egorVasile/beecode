"""Model catalogue sync: ask every provider what it serves today.

Endpoints rewrite their catalogues without a release (crax did, twice), and
the picker offering names that answer `Unknown model` reads as "the models
do not answer". So every start pings every provider that can list its
models, diffs the answer against the last saved list, and — only when
something changed — shows what was added and removed per provider and asks
whether to save.

Nothing here blocks the interface for long: providers are asked in parallel
(4 workers, 8 s each), a provider that does not answer is skipped silently,
and the whole check is off with `BEECODE_MODEL_SYNC=0`.
"""
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from beeagent.i18n import L

PER_PROVIDER_TIMEOUT = 8.0
MAX_WORKERS = 4


@dataclass
class ModelDiff:
    provider: str
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    count: int = 0

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def _cache_path(workdir: str, name: str):
    from pathlib import Path

    return Path(workdir) / ".beeagent" / f"models_{str(name).strip().lower()}.json"


def _read_cache(workdir: str, name: str) -> list[str] | None:
    """The last saved list, or None when no baseline exists yet."""
    import json

    try:
        data = json.loads(_cache_path(workdir, name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    models = [str(m) for m in data.get("models", []) if str(m).strip()]
    return models


def _write_cache(workdir: str, name: str, models: list[str]) -> None:
    """Save the verified list. The config stays the truth; this is the diff base."""
    import json

    from beeagent import __version__

    if not models:
        return
    try:
        path = _cache_path(workdir, name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"saved_at": time.time(), "version": __version__,
                                    "models": list(models)}), encoding="utf-8")
    except OSError:
        pass


def _live_models(agent, provider, name: str) -> list[str] | None:
    """What the endpoint serves right now, or None when it would not say."""
    discover = getattr(provider, "discover_models", None)
    if callable(discover):
        try:
            models = discover()
            models = [str(m) for m in (models or []) if str(m).strip()]
            return models or None
        except Exception:
            return None
    # An OpenAI-compatible endpoint without a discover method: ask /v1/models.
    if type(provider).__name__ == "OpenAICompatProvider":
        try:
            from beeagent.core import provider_setup

            models, _, _ = provider_setup.discover(
                getattr(provider, "base_url", ""),
                getattr(provider, "api_key", "") or "",
                timeout=PER_PROVIDER_TIMEOUT)
            models = [str(m) for m in (models or []) if str(m).strip()]
            return models or None
        except Exception:
            return None
    return None


def _check_one(agent, workdir: str, name: str) -> ModelDiff | None:
    """Diff one provider. None means "nothing to say" (no answer, or new baseline)."""
    provider = agent.providers.get(name)
    if provider is None:
        return None
    live = _live_models(agent, provider, name)
    if not live:
        return None
    known = _read_cache(workdir, name)
    if known is None:
        # First sight: establish the baseline quietly, report nothing.
        declared = [str(m) for m in (getattr(provider, "models", None) or [])
                    if str(m).strip()]
        _write_cache(workdir, name, live or declared)
        return None
    known_set, live_set = set(known), set(live)
    diff = ModelDiff(provider=name, count=len(live),
                     added=sorted(live_set - known_set),
                     removed=sorted(known_set - live_set))
    return diff if diff.changed else None


def check_all(agent, workdir: str = ".",
              timeout: float = PER_PROVIDER_TIMEOUT) -> list[ModelDiff]:
    """Ask every provider, in parallel. Never raises; silence is a skip."""
    if os.environ.get("BEECODE_MODEL_SYNC") == "0":
        return []
    names = []
    try:
        names = agent.providers.list_names()
    except Exception:
        return []
    diffs: list[ModelDiff] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_check_one, agent, workdir, n): n for n in names}
        for future, name in futures.items():
            try:
                diff = future.result(timeout=timeout + 2)
            except Exception:
                continue
            if diff is not None:
                diffs.append(diff)
    return sorted(diffs, key=lambda d: d.provider)


def report(diffs: list[ModelDiff]) -> str:
    """One human-readable block: per provider, how many, what came and went."""
    lines = []
    for diff in diffs:
        lines.append(L(
            f"provider {diff.provider}: {diff.count} models "
            f"(+{len(diff.added)} new, -{len(diff.removed)} removed)",
            f"провайдер {diff.provider}: моделей {diff.count} "
            f"(+{len(diff.added)} новых, -{len(diff.removed)} убрано)"))
        if diff.added:
            lines.append(L(f"  + added: {', '.join(diff.added)}",
                           f"  + добавлены: {', '.join(diff.added)}"))
        if diff.removed:
            lines.append(L(f"  - removed: {', '.join(diff.removed)}",
                           f"  - убраны: {', '.join(diff.removed)}"))
    return "\n".join(lines)


def startup_check(agent, workdir: str = ".", interactive: bool = True) -> None:
    """Run at start: check, print the report, ask to save.

    Classic REPL only — it owns stdin. Everywhere else use `check_all` and
    `save_diffs` directly (the TUI note path does exactly that).
    """
    diffs = check_all(agent, workdir)
    if not diffs:
        return
    print(report(diffs))
    if not interactive:
        return
    try:
        import sys

        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            # Piped start: report, then save — nobody can answer here.
            _save_live(agent, workdir, diffs)
            print(L("saved (no terminal to ask).", "сохранено (спросить не у кого)."))
            return
        answer = input(L("save the new lists? [Y/n] ",
                         "сохранить новые списки? [Y/n] ")).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return
    if answer in ("", "y", "yes", "д", "да"):
        _save_live(agent, workdir, diffs)
        print(L("saved.", "сохранено."))
    else:
        print(L("kept the old lists.", "оставлены старые списки."))


def _save_live(agent, workdir: str, diffs: list[ModelDiff]) -> None:
    """Re-fetch is wasteful; the check already has the lists — almost.

    `check_all` returns diffs only, so re-ask each changed provider once,
    briefly: a catalogue that moved mid-start is rarer than a stale write.
    """
    for diff in diffs:
        provider = agent.providers.get(diff.provider)
        if provider is None:
            continue
        live = _live_models(agent, provider, diff.provider)
        if live:
            _write_cache(workdir, diff.provider, live)
