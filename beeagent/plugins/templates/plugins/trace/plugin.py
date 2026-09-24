"""The last failures, in a file — because an empty 502 body blamed the wrong thing.

The pool's database socket died and every request came back 502 with no body at
all. Each client message read like a model problem, the model list was fine, and
it took hours to find out that no model had ever been asked. BeeCode sees those
errors as they happen, so `trace` writes them to `.beeagent/trace.jsonl`: model,
provider, HTTP status, a short error, timestamp. `/trace` prints the tail and one
line about what to do next.

Message bodies never reach the file — a log you have to redact before sharing is
a log nobody shares, and the cause is in the status and the model anyway.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from beeagent.i18n import L

MAX_LINES = 200
ERROR_CHARS = 200
SHOWN = 8

# Only a 4xx/5xx reads as a status here; a bare number in an error text is a
# token count or a port, and printing it as an HTTP code sends people debugging
# the wrong layer.
STATUS = re.compile(r"\b([45]\d\d)\b")

SERVER_SIDE = (500, 502, 503, 504)


def root(api) -> Path:
    agent = getattr(api, "agent", None)
    return Path(getattr(agent, "workdir", ".") or ".")


def path(api) -> Path:
    return root(api) / ".beeagent" / "trace.jsonl"


def _short(text: object) -> str:
    """One line, capped: the reason, not the transcript."""
    return " ".join(str(text or "").split())[:ERROR_CHARS]


def _status(text: str) -> int | None:
    found = STATUS.search(text or "")
    return int(found.group(1)) if found else None


def _entry(api, kind: str, error: str) -> dict:
    config = getattr(api, "config", None)
    return {
        "at": time.time(),
        "kind": kind,
        "model": str(getattr(config, "model", "") or ""),
        "provider": str(getattr(config, "provider", "") or ""),
        "status": _status(error),
        "error": _short(error),
    }


def append(api, entry: dict) -> None:
    """One more line, and the file kept inside its cap.

    Written through a temporary file and renamed: a session killed mid-write used
    to leave a torn last line, and a reader that chokes on its own log is worse
    than no log at all.
    """
    target = path(api)
    try:
        lines = read(api)[-MAX_LINES + 1:]
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".tmp")
        payload = [json.dumps(line, ensure_ascii=False) for line in lines]
        payload.append(json.dumps(entry, ensure_ascii=False))
        tmp.write_text("\n".join(payload) + "\n", encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass        # a trace that cannot be written must not break the session


def read(api) -> list[dict]:
    """Every recorded failure, oldest first. A line that will not parse is skipped."""
    target = path(api)
    if not target.is_file():
        return []
    try:
        raw = target.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in raw:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            entries.append(record)
    return entries


def setup(api) -> None:
    def watch(event: str, data: dict) -> None:
        if event == "error":
            append(api, _entry(api, "error", str(data.get("message") or "")))
        elif event == "retry":
            attempt = data.get("attempt")
            append(api, _entry(api, "retry", L(
                f"attempt {attempt} of 3 got no usable answer",
                f"попытка {attempt} из 3 не дала ответа")))

    api.event("error", watch)
    api.event("retry", watch)

    def trace(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.console import Group
        from rich.text import Text

        entries = read(api)
        if not entries:
            return CommandResult(output=Text(L(
                "nothing has failed on this machine — errors and retries are written to "
                ".beeagent/trace.jsonl as they happen",
                "на этой машине пока ничего не падало — ошибки и повторы пишутся в "
                ".beeagent/trace.jsonl по мере того как случаются"), style="dim"))
        wanted = _count(args)
        return CommandResult(output=Group(_table(entries[-wanted:]),
                                          Text(_advice(entries), style="bold yellow")))

    api.command("trace", "The last failures this machine actually saw", trace,
                usage="/trace [n]")


def _count(args) -> int:
    raw = " ".join(args).strip()
    try:
        return max(1, min(MAX_LINES, int(raw)))
    except ValueError:
        return SHOWN


def _advice(entries: list[dict]) -> str:
    """One line about where to look, from the statuses rather than the prose."""
    server = [e for e in entries if e.get("status") in SERVER_SIDE]
    limited = [e for e in entries if e.get("status") == 429]
    silent = [e for e in entries if not e.get("status")]
    last = entries[-1]
    pool_side = [e for e in server if e.get("provider") == "pool"]

    if pool_side:
        names = sorted({str(e.get("model") or "?") for e in pool_side})
        return L(f"{len(pool_side)} of {len(entries)} failures came back {pool_side[-1]['status']} "
                 f"from the pool itself, across {', '.join(names)} — the models were never "
                 f"reached, so look at the pool before changing one",
                 f"{len(pool_side)} из {len(entries)} сбоев вернул сам пул кодом "
                 f"{pool_side[-1]['status']} на моделях {', '.join(names)} — до моделей не "
                 f"дошло, сначала смотри пул")
    if limited:
        return L(f"{len(limited)} failures were rate limits — /seat shows what is left of "
                 f"today's budget, /provider g4f answers without a seat",
                 f"{len(limited)} сбоев — это лимиты: /seat покажет остаток дневной нормы, "
                 f"/provider g4f отвечает и без места")
    if silent and len(silent) == len(entries):
        return L("no HTTP status in any line, so the request never got an answer to read — "
                 "start with the newest row and the model named there",
                 "ни в одной строке нет HTTP-кода: запрос не получил ответа, с которого "
                 "стоит начинать — последняя строка и названная в ней модель")
    return L(f"newest failure: {last.get('kind')} on {last.get('model') or '?'} via "
             f"{last.get('provider') or '?'} — /trace {min(MAX_LINES, len(entries))} for more",
             f"последний сбой: {last.get('kind')} на {last.get('model') or '?'} через "
             f"{last.get('provider') or '?'} — /trace {min(MAX_LINES, len(entries))} покажет больше")


def _table(entries: list[dict]):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title=f"🐝 /trace — {len(entries)} of the last failures", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column("when", style="dim")
    table.add_column("what")
    table.add_column("model")
    table.add_column("provider", style="dim")
    table.add_column("http", justify="right")
    table.add_column("error")
    for entry in entries:
        table.add_row(
            time.strftime("%m-%d %H:%M", time.localtime(entry.get("at", 0))),
            str(entry.get("kind", "")),
            str(entry.get("model", ""))[:28],
            str(entry.get("provider", ""))[:12],
            str(entry.get("status") or "-"),
            str(entry.get("error", ""))[:60])
    return table
