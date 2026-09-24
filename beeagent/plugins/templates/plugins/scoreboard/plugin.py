"""What each model did here, rather than what a provider advertises it has.

One afternoon of measurements: qwen3.5 through qwen3.8 answer 502, gpt-4 refuses
anything over 2048 tokens, grok-code-fast-1 spends 23.7 s on six words, and LLM7
answers the id `default` alone even though its list names 44. None of that is
written anywhere a later session can read it. `scoreboard` keeps it locally:
every answer, retry, failure and forced substitution lands in `.beeagent/board.json`,
and `/board` ranks models by what this machine has personally seen work, with the
median of the latencies it measured — median, because one cold-start wait should
not make a fast model look slow.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from statistics import median

from beeagent.i18n import L

SAMPLES = 12          # per model: a month of afternoons, not a history of the box
NOTE_CHARS = 80
SHOWN = 20


def root(api) -> Path:
    agent = getattr(api, "agent", None)
    return Path(getattr(agent, "workdir", ".") or ".")


def path(api) -> Path:
    return root(api) / ".beeagent" / "board.json"


def _blank() -> dict:
    return {"ok": 0, "fail": 0, "retries": 0, "last": "", "note": "",
            "refused": False, "lat": [], "when": 0.0}


def read(api) -> dict:
    """The tally as it stands. Anything unreadable starts the count again."""
    target = path(api)
    if not target.is_file():
        return {}
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    board = {}
    for name, row in stored.items():
        if not isinstance(name, str) or not isinstance(row, dict):
            continue
        fresh = _blank()
        fresh.update({key: row[key] for key in fresh if key in row})
        fresh["ok"] = int_or(fresh["ok"], 0)
        fresh["fail"] = int_or(fresh["fail"], 0)
        fresh["retries"] = int_or(fresh["retries"], 0)
        fresh["refused"] = bool(fresh["refused"])
        fresh["when"] = float(fresh["when"]) if isinstance(fresh["when"], (int, float)) else 0.0
        samples = fresh["lat"]
        fresh["lat"] = ([float(s) for s in samples if isinstance(s, (int, float))]
                        if isinstance(samples, list) else [])
        board[name] = fresh
    return board


def write(api, board: dict) -> None:
    target = path(api)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / (target.name + ".tmp")
        tmp.write_text(json.dumps(board, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        pass        # a tally nobody can write is not a reason to stop answering


def int_or(value, fallback: int) -> int:
    return value if isinstance(value, int) else fallback


def _median(row: dict):
    return round(median(row["lat"]), 2) if row["lat"] else None


def _short(text: object) -> str:
    return " ".join(str(text or "").split())[:NOTE_CHARS]


def setup(api) -> None:
    # `status` is fired once per model call, before the request leaves, so it is
    # the only honest place to start the stopwatch — and the interval it measures
    # is one answer rather than a whole task with its tool runs in it.
    turn = {"model": "", "started": 0.0}

    def watch(event: str, data: dict) -> None:
        config = getattr(api, "config", None)
        if event == "status":
            turn["model"] = str(getattr(config, "model", "") or "")
            turn["started"] = time.monotonic()
            return

        board = read(api)
        if event == "model_switched":
            wanted = str(data.get("from") or "").strip()
            turn["model"] = str(data.get("to") or "").strip()
            if wanted:
                # The provider listed the name and could not answer for it: that is
                # the single most useful thing this board knows about a model.
                row = board.setdefault(wanted, _blank())
                row["refused"] = True
                row["last"] = "refused"
                row["when"] = time.time()
            write(api, board)
            return

        name = turn["model"] or str(getattr(config, "model", "") or "") or L("unknown", "неизвестна")
        row = board.setdefault(name, _blank())
        if event == "done":
            row["ok"] += 1
            row["last"] = "ok"
            if turn["started"]:
                row["lat"] = (row["lat"] + [round(time.monotonic() - turn["started"], 2)])[-SAMPLES:]
            turn["started"] = 0.0
        elif event == "error":
            row["fail"] += 1
            row["last"] = "error"
            row["note"] = _short(data.get("message"))
        elif event == "retry":
            row["retries"] += 1
            row["last"] = "retry"
        else:
            return
        row["when"] = time.time()
        write(api, board)

    for name in ("status", "done", "error", "retry", "model_switched"):
        api.event(name, watch)

    def board(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        rows = read(api)
        if not rows:
            return CommandResult(output=Text(L(
                "nothing measured yet — ask BeeCode something, and /board will rank the "
                "models that answered on this machine",
                "измеренного пока нет — спроси BeeCode, и /board расставит модели по тому, "
                "что ответило на этой машине"), style="dim"))
        return CommandResult(output=_table(rows))

    api.command("board", "Rank models by what worked on this machine", board, usage="/board")


def _order(item) -> tuple:
    name, row = item
    return (bool(row["refused"]), -(row["ok"] - row["fail"]),
            _median(row) if _median(row) is not None else 1e9, name.lower())


_VERDICT = {
    "ok": ("answered last", "отвечала последней"),
    "error": ("failed last", "падала последней"),
    "retry": ("still retrying", "всё ещё повторяет"),
    "refused": ("not served here", "здесь не отдаётся"),
}


def _table(board: dict):
    from rich.table import Table

    from beeagent.ui import skin

    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title="🐝 /board — measured on this machine, not from a model list",
                  show_header=True, header_style="bold #ffcc00", **kwargs)
    table.add_column("model")
    table.add_column("ok", justify="right", style="bold #ffcc00")
    table.add_column("fail", justify="right", style="dim")
    table.add_column("retry", justify="right", style="dim")
    table.add_column("median", justify="right")
    table.add_column("last seen", style="dim")
    table.add_column("verdict")

    for name, row in sorted(board.items(), key=_order)[:SHOWN]:
        middle = _median(row)
        when = row["when"]
        verdict = L(*_VERDICT.get(row["last"], ("seen", "наблюдались")))
        if row["last"] == "error" and row["note"]:
            verdict = f"{verdict}: {row['note'][:40]}"
        table.add_row(
            name, str(row["ok"]), str(row["fail"]), str(row["retries"]),
            f"{middle}s" if middle is not None else "—",
            time.strftime("%m-%d %H:%M", time.localtime(when)) if when else "—",
            verdict)
    return table
