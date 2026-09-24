"""How much of today's pool budget is left — before the 429 says it for you.

A seat on the pool spends 200 requests and 400 000 estimated tokens a day, and
running out was a silent affair: the only number anyone saw was the
`daily_limit_exceeded` 429 that arrived in the middle of a task. `/seat` asks the
pool for this seat's own counters (`GET /v1/seat`) and answers in what remains
rather than what was spent, with the reset spelled as "in 4h 12m".

Everything that can go wrong with a status command goes wrong here first: the
endpoint may not exist yet, the free instance may be asleep, the config may hold
no seat at all. Each of those is one readable line, never a traceback.
"""
from __future__ import annotations

from urllib.parse import urlsplit

import httpx

from beeagent.i18n import L

# What the pool promises on `/v1/seat`. A body missing any of them is not a seat
# report, and saying so beats printing "None of 0 left".
COUNTERS = ("requests", "requests_limit", "tokens", "tokens_limit")

# A seat report is one line of JSON on a box that does no work to answer it; the
# only honest reason to wait longer is a platform that wakes on the first request.
TIMEOUT = 12.0


class SeatError(Exception):
    """The pool could not report this seat. The message is for the user as is."""


def fetch_seat(url: str, token: str, timeout: float = TIMEOUT) -> dict:
    """Ask the pool what this seat has used today. Raises `SeatError`, always readable."""
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url.rstrip("/") + "/v1/seat",
                                  headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise SeatError(L(
            f"the pool at {url} did not answer ({e.__class__.__name__}) — a free instance "
            f"sleeps when nobody uses it, so ask again in a moment",
            f"пул по адресу {url} не ответил ({e.__class__.__name__}) — бесплатный "
            f"инстанс засыпает без обращений, спросишь через минуту")) from e

    if response.status_code == 404:
        raise SeatError(L(
            f"the pool at {url} has no /v1/seat endpoint yet — the budget report needs it",
            f"у пула по адресу {url} пока нет /v1/seat — для отчёта о норме нужна эта ручка"))

    body = _json_object(response)
    if response.status_code != 200:
        reason = str((body or {}).get("error") or "").strip()
        raise SeatError(L(
            f"the pool answered {response.status_code}" + (f" — {reason}" if reason else ""),
            f"пул ответил {response.status_code}" + (f" — {reason}" if reason else "")))
    if body is None:
        # An empty body is what a broken upstream looks like. Say which side broke.
        raise SeatError(L(
            f"the pool answered 200 with a body that is not JSON — that is the pool's "
            f"failure, not this install's",
            f"пул ответил 200 телом, которое не JSON — это поломка пула, а не этой сборки"))
    try:
        report = {key: int(body[key]) for key in COUNTERS}
    except (KeyError, TypeError, ValueError) as e:
        raise SeatError(L(
            "the pool's seat report does not carry the counters it promises "
            f"({', '.join(COUNTERS)})",
            "отчёт о месте не содержит обещанных чисел "
            f"({', '.join(COUNTERS)})")) from e
    report["resets_in_seconds"] = _maybe_int(body.get("resets_in_seconds"))
    return report


def _json_object(response) -> dict | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _maybe_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _remaining(seconds) -> str:
    """The reset as a person reads it: "4h 12m", not 15120."""
    if seconds is None:
        return L("unknown", "неизвестно")
    left = max(0, int(seconds))
    hours, minutes = left // 3600, (left % 3600) // 60
    if hours and minutes:
        return L(f"{hours}h {minutes}m", f"{hours} ч {minutes} мин")
    if hours:
        return L(f"{hours}h", f"{hours} ч")
    if minutes:
        return L(f"{minutes}m", f"{minutes} мин")
    return L(f"{left % 60}s", f"{left % 60} с")


def setup(api) -> None:
    api.setting("timeout", TIMEOUT, "seconds to wait for the pool to answer /v1/seat")

    def seat(ctx, args):
        from beeagent.ui.commands import CommandResult
        from rich.text import Text

        url = str(getattr(api.config, "pool_url", "") or "").strip()
        token = str(getattr(api.config, "pool_token", "") or "").strip()
        if not url:
            return CommandResult(output=Text(L(
                "the pool has no address — /pool url https://…",
                "у пула нет адреса — /pool url https://…"), style="bold yellow"))
        if not token:
            return CommandResult(output=Text(L(
                f"this install has no seat at {url} yet — /pool enroll takes one",
                f"у этой сборки нет места в пуле {url} — /pool enroll его возьмёт"),
                style="bold yellow"))

        try:
            report = fetch_seat(url, token, float(api.get("timeout") or TIMEOUT))
        except SeatError as e:
            return CommandResult(output=Text(str(e), style="bold yellow"))
        except Exception as e:
            return CommandResult(output=Text(L(
                f"the seat check did not finish: {e.__class__.__name__}",
                f"проверка места не завершилась: {e.__class__.__name__}"),
                style="bold yellow"))
        return CommandResult(output=_report(url, report))

    api.command("seat", "How much of today's pool budget is left", seat, usage="/seat")


def _report(url: str, report: dict):
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    from beeagent.ui import skin

    host = urlsplit(url).hostname or url
    left = {key: max(0, report[f"{key}_limit"] - report[key])
            for key in ("requests", "tokens")}
    kwargs = skin.frame_kwargs("#ffcc00")
    table = Table(title=f"🐝 seat at {host}", show_header=True,
                  header_style="bold #ffcc00", **kwargs)
    table.add_column(L("today", "сегодня"))
    table.add_column(L("left", "осталось"), justify="right", style="bold #ffcc00")
    table.add_column(L("of", "из"), justify="right", style="dim")
    table.add_column(L("spent", "израсходовано"), justify="right", style="dim")
    for key, label in (("requests", L("requests", "запросы")),
                       ("tokens", L("tokens", "токены"))):
        table.add_row(label, f"{left[key]:,}", f"{report[f'{key}_limit']:,}",
                      f"{report[key]:,}")

    lines = [table, Text(L(f"resets in {_remaining(report['resets_in_seconds'])} (midnight UTC)",
                           f"обновится через {_remaining(report['resets_in_seconds'])} "
                           f"(полночь по UTC)"), style="dim")]
    if left["requests"] == 0 or left["tokens"] == 0:
        lines.append(Text(L(
            "today's budget is spent — /provider g4f answers without a seat, or wait for the reset",
            "дневная норма выбрана — /provider g4f отвечает без места в пуле, "
            "или жди полуночи"), style="bold yellow"))
    return Group(*lines)
