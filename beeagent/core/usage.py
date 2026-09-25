"""What this conversation cost, in the only units BeeCode has.

BeeCode's promise is that models are free: g4f routes cost nothing per call and a
pool seat is a daily budget, not a price per token. So the number worth showing is
never a sum of money — it is requests made, tokens spent, and how much of the
seat's allowance is left, *before* the limit closes in the middle of a task. This
module keeps that ledger in `.beeagent/usage.json`: read-only at boot, written
only once a turn has actually happened, and never the reason a turn fails.

Three promises the rest of the file is organised around:

* **Nothing is invented.** Tokens are measured with the same ruler
  `core/windows.py` budgets a prompt with — `utils.tokens.count_tokens` — and a
  count the endpoint reported itself beats an estimate and is marked
  differently: `✔` for the provider's own number, `~` for ours, the two marks
  `/models` already uses for a context window. One visual language.
* **Nothing is lost quietly.** A torn or hand-ruined file is moved to
  `usage.json.broken` instead of being overwritten (the same rule
  `config/loader._set_aside` applies to a bad config), the ledger starts clean,
  and it says so once — at the next `/stats`, because this file has no right to
  print at boot. The half-written file keeps the old totals for whoever wants
  them back.
* **Nothing grows forever.** `MAX_ROWS` model rows and `MAX_SESSIONS`
  conversations are kept; the ones nobody has touched in the longest fall off,
  and the count that fell off is announced.

A cache hit is deliberately not a request: the whole point of economy mode is
that nothing was asked, and counting a hit as a request would report a seat as
spent when it was not.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from beeagent.i18n import L
from beeagent.utils.tokens import count_tokens

# Where the ledger lives: module-level and relative, so a test (or a second
# install) can point it elsewhere, exactly like `core/windows.py` does with its
# own measured-window cache. The path is read at call time, never import time.
PATH = Path(".beeagent") / "usage.json"

VERSION = 1

# The same `model@provider` spelling `core/windows.py` keys its measurements by:
# one model id served through two endpoints is two rows, because the two cost
# very different amounts of the same request.
SEP = "@"

# The file may not grow without limit. On the phone in the pocket, six hundred
# rows of history is a megabyte rewritten after every answer, and a ledger that
# slows the turn it is measuring has the priorities wrong.
MAX_ROWS = 40
MAX_SESSIONS = 20

# How many rows `/stats` prints before it says "and N more". A table nobody can
# read in one screen is a list.
TOP_ROWS = 8

# What a broken ledger looks like: an unreadable or half-written file, a folder
# deleted underneath us, a number a hand-edited file wrote as "many". None of
# them is a reason to lose a turn.
LEDGER_FAILURES = (OSError, ValueError, TypeError, KeyError, AttributeError)

# The eight things a row counts. `reported`/`estimated` are not extra spending:
# they say which half of the token numbers below them is the endpoint's word and
# which half is ours.
COUNTERS = ("requests", "prompt_tokens", "completion_tokens", "cache_hits",
            "stream_seconds", "errors", "reported", "estimated")

# The three things that happen to a request, and the only three `note()` takes.
KINDS = ("answer", "error", "cache")

# A turn whose caller never said which conversation it belongs to. Sessions are
# keyed by `Session.session_id`; this is the one drawer anonymous turns go in.
ANON = "-"

_DATA: dict | None = None
_STAMP: tuple | None = None
_NOTICES: list[str] = []
_TURNS: dict[str, float] = {}
_TRIM: dict = {"count": 0, "text": ""}


# --- numbers that cannot lie ------------------------------------------------

def _whole(value) -> int:
    """A count, or 0. A string, a float or a bool never becomes a token count."""
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value)) if value == value else 0
    if isinstance(value, str):
        try:
            return max(0, int(float(value.strip())))
        except ValueError:
            return 0
    return 0


def _seconds(value) -> float:
    """A duration that makes sense. NaN, a negative and "later" are all 0."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out == out and out > 0 else 0.0


def _describe(exc: BaseException) -> str:
    """Something printable for any exception, including the empty ones."""
    text = str(exc).strip()
    return text or exc.__class__.__name__


# --- the shape of the file --------------------------------------------------

def _row(model: str = "", provider: str = "") -> dict:
    out = {name: 0 for name in COUNTERS}
    out["stream_seconds"] = 0.0
    out["model"] = model
    out["provider"] = provider
    out["last_seen"] = 0.0
    out["last_error"] = ""
    return out


def _key(model: str, provider: str) -> str:
    model = str(model or "")
    provider = str(provider or "")
    return f"{model}{SEP}{provider}" if provider else model


def _split(key: str) -> tuple[str, str]:
    """The model and the endpoint a stored row belongs to.

    Read off the row when it says, and out of the key when it does not — a record
    written by a future or hand-edited file still has to be attributed to
    something rather than printed as a blank model name.
    """
    return (key.split(SEP, 1) + [""])[:2] if SEP in key else (key, "")


def _adopt(raw: object) -> dict:
    """A parsed file, made safe to add to.

    Every value is re-read through `_whole` rather than trusted: one field a text
    editor turned into `"many"` must not survive as a string that the next `+=`
    raises inside a turn.
    """
    data = {
        "version": VERSION, "updated": 0.0, "trimmed": 0,
        "rows": {}, "sessions": {},
    }
    if not isinstance(raw, dict):
        return data
    data["updated"] = _seconds(raw.get("updated"))
    data["trimmed"] = _whole(raw.get("trimmed"))
    dropped = 0
    for name, limit in (("rows", MAX_ROWS), ("sessions", MAX_SESSIONS)):
        stored = raw.get(name)
        if not isinstance(stored, dict):
            dropped += len(stored) if isinstance(stored, (list, tuple)) else 0
            continue
        for key, entry in list(stored.items())[:limit]:
            if not isinstance(entry, dict):
                dropped += 1
                continue
            row = _row(*_split(str(key)))
            for counter in COUNTERS:
                row[counter] = (_seconds(entry.get(counter)) if counter == "stream_seconds"
                                else _whole(entry.get(counter)))
            row["last_seen"] = _seconds(entry.get("last_seen"))
            row["last_error"] = str(entry.get("last_error") or "")[:200]
            if name == "sessions":
                row["model"] = ""
                row["provider"] = ""
            data[name][str(key)] = row
    if dropped:
        _NOTICES.append(L(
            f"⚠ {PATH.name} held {dropped} entr"
            f"{'y' if dropped == 1 else 'ies'} BeeCode could not read — they were "
            f"dropped, the rest of the ledger is intact",
            f"⚠ в {PATH.name} было записей, не читавшихся программой: {dropped} — "
            f"они убраны, остальной учёт цел"))
    return data


def _skeleton() -> dict:
    return _adopt({})


# --- the file ---------------------------------------------------------------

def _identity() -> tuple:
    """What the ledger file is right now: path, mtime, size.

    The absolute path is part of it on purpose, so a test that changes directory
    cannot be served another directory's cached numbers.
    """
    try:
        absolute = str(PATH.absolute())
    except OSError:                       # a cwd that no longer exists
        absolute = str(PATH)
    try:
        stat = PATH.stat()
        return (absolute, stat.st_mtime_ns, stat.st_size)
    except OSError:
        return (absolute, 0, 0)


def _set_aside() -> None:
    """Keep the damaged ledger beside the new one rather than under it.

    A file we could not read is nobody's empty history: it goes to one side, the
    way `load_config` sends an unreadable config, and an existing rescue is never
    overwritten — the first one is somebody's totals too.
    """
    broken = PATH.with_name(PATH.name + ".broken")
    target = broken
    step = 1
    while target.exists():
        target = broken.with_name(f"{broken.name}.{step}")
        step += 1
    try:
        PATH.replace(target)
    except OSError:
        try:
            PATH.unlink(missing_ok=True)
        except OSError:
            pass                          # reported either way; the turn goes on


def read() -> dict:
    """The ledger, from memory while the file underneath it has not changed.

    Reading never creates a directory and never writes: `/stats`, `/help` and
    every other read-only surface must be able to ask this in a checkout without
    leaving a file behind.
    """
    global _DATA, _STAMP
    identity = _identity()
    if _DATA is not None and identity == _STAMP:
        return _DATA
    if identity[1] == 0 and not PATH.exists():
        _DATA, _STAMP = _skeleton(), identity
        return _DATA
    try:
        raw = json.loads(PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(L("it holds no object", "в нём нет объекта"))
    except LEDGER_FAILURES as exc:
        _NOTICES.append(L(
            f"⚠ {PATH} was unreadable ({_describe(exc)}) — the count starts again "
            f"from nothing. The damaged file is kept as {PATH.name}.broken: the "
            f"totals in it are not gone, they are just not readable by BeeCode",
            f"⚠ {PATH} не читается ({_describe(exc)}) — счёт начинается заново. "
            f"Повреждённый файл сохранён как {PATH.name}.broken: суммы из него не "
            f"потеряны, просто BeeCode их не прочитает"))
        _set_aside()
        _DATA, _STAMP = _skeleton(), _identity()
        return _DATA
    _DATA, _STAMP = _adopt(raw), identity
    return _DATA


def _write(data: dict) -> bool:
    """One atomic swap, or the honest reason it did not happen.

    The same lesson `save_config` and `Session.save` learned: writing in place
    leaves half a file after a crash or Ctrl+C, and the next boot reads a ledger
    that says nothing. The in-memory copy keeps this session's numbers whatever
    the disk does, so a read-only folder costs the history and not the totals.
    """
    global _STAMP
    try:
        PATH.parent.mkdir(parents=True, exist_ok=True)
        descriptor, tmp_name = tempfile.mkstemp(dir=str(PATH.parent),
                                                prefix=PATH.name + ".", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(data, handle, indent=2, sort_keys=True, ensure_ascii=False)
            os.replace(tmp_name, PATH)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.remove(tmp_name)
                except OSError:
                    pass
        _STAMP = _identity()
        return True
    except LEDGER_FAILURES as exc:
        _NOTICES.append(L(
            f"⚠ the usage ledger could not be saved to {PATH} ({_describe(exc)}) — "
            f"the numbers below still add up for this session, but they will not "
            f"be here after it ends",
            f"⚠ не удалось сохранить учёт в {PATH} ({_describe(exc)}) — цифры ниже "
            f"верны для этого разговора, но после его окончания их здесь не будет"))
        return False


def _trim(data: dict, keep=()) -> int:
    """Bring the history back inside both caps, oldest first out.

    `keep` names the rows this turn is writing: neither the model in use nor the
    conversation the user is in can be the one that fell off to make room for it.
    """
    held = {str(name) for name in keep if name}
    dropped = 0
    for name, limit in (("rows", MAX_ROWS), ("sessions", MAX_SESSIONS)):
        stored = data[name]
        if len(stored) <= limit:
            continue
        order = sorted(((_seconds(row.get("last_seen")), key)
                        for key, row in stored.items() if key not in held))
        for _, key in order[:len(stored) - limit]:
            stored.pop(key, None)
            dropped += 1
    if dropped:
        data["trimmed"] = _whole(data.get("trimmed")) + dropped
        # One line for the whole session's trimming, not one per write: a project
        # that asks five hundred different models would otherwise be told about its
        # own bookkeeping fifty times. The lifetime count lives in the file and is
        # printed as its own row regardless of whether this line was ever read.
        _TRIM["count"] += dropped
        _TRIM["text"] = _trim_line(_TRIM["count"])
    return dropped


def _trim_line(count: int) -> str:
    return L(
        f"the ledger keeps the {MAX_ROWS} models and {MAX_SESSIONS} conversations "
        f"it has spent the most requests on; {count} "
        f"{'row was' if count == 1 else 'rows were'} dropped to hold the line",
        f"учёт хранит {MAX_ROWS} моделей и {MAX_SESSIONS} разговоров; "
        f"{count} {'запись стёрта' if count == 1 else 'записей стёрто'}, "
        f"чтобы не разрастаться")


# --- what the user is told, once --------------------------------------------

def take_notice() -> str:
    """The next thing the ledger has to confess, or "".

    Given out one at a time and never twice for the same event: an economy cache
    that says it broke on every request is economy mode interrupting the user for
    its own bookkeeping. Notices are asked for by `/stats`, which is the only
    place this module is allowed to speak.
    """
    if _NOTICES:
        return _NOTICES.pop(0)
    if _TRIM["text"]:
        text = _TRIM["text"]
        _TRIM["text"] = ""
        _TRIM["count"] = 0
        return text
    return ""


def notices() -> tuple[str, ...]:
    """What is still to be said, without saying it."""
    return tuple(_NOTICES)


def forget() -> None:
    """Drop everything this module holds: the cached ledger and its unsaid lines.

    A second BeeCode window in the same folder writes the same ledger, and the
    one that has not written since is holding numbers the file no longer has — so
    this is a real recovery path, and also the isolation hook a test needs.
    """
    global _DATA, _STAMP
    _DATA, _STAMP = None, None
    _TURNS.clear()
    _NOTICES.clear()
    _TRIM["count"] = 0
    _TRIM["text"] = ""


# --- recording --------------------------------------------------------------

def tick(session: str = "") -> None:
    """Stamp the moment a request goes out, so the answer can say how long it took.

    Nothing else times a turn, and the alternative — working the wait out from
    when the last note arrived — would bill the tool runs and the seconds the user
    spent reading the answer as streaming time. With no tick in front of a note
    the duration stays 0 and `/stats` prints "—": a missing number, not a made-up
    one.
    """
    try:
        _TURNS[_sid(session)] = time.time()
    except Exception:
        pass


def _sid(session: str) -> str:
    return str(session or "").strip() or ANON


def _reported(usage: object) -> tuple[int, int] | None:
    """The endpoint's own token counts, when it gave both of them.

    Half a report is not a report: an answer that says how much came back and not
    what went out would be marked `✔` while half its bytes are still a guess.
    """
    if not isinstance(usage, dict):
        return None
    prompt = _whole(usage.get("prompt_tokens") or usage.get("input_tokens"))
    completion = _whole(usage.get("completion_tokens") or usage.get("output_tokens"))
    if not prompt or not completion:
        return None
    return prompt, completion


def _count_prompt(messages: object, model: str) -> int:
    """What the request cost to send, counted the way `/token` counts it.

    Summing the message texts rather than the `json.dumps()` of the whole array:
    that spelling escapes every non-ASCII character into `\\uXXXX` and charges
    about three tokens for a Cyrillic letter the model never saw.
    """
    # The one thing this estimate is wrong about: `count_tokens` prices by
    # character class with weights measured against cl100k, so on the Android
    # path (no tiktoken wheel, no compiler) it under-reads emoji and CJK for the
    # small-vocab models a phone actually reaches — they split both finer.
    if isinstance(messages, (list, tuple)):
        total = 0
        for message in messages:
            if isinstance(message, dict):
                total += count_tokens(str(message.get("content") or ""), model)
            elif isinstance(message, str):
                total += count_tokens(message, model)
        return total
    return count_tokens(str(messages or ""), model)


def _add(row: dict, *, prompt: int = 0, completion: int = 0, requests: int = 0,
         hits: int = 0, seconds: float = 0.0, errors: int = 0,
         reported: int = 0, estimated: int = 0, note: str = "") -> None:
    """One row, moved forward. Every field that is not given is left alone."""
    row["requests"] += requests
    row["prompt_tokens"] += prompt
    row["completion_tokens"] += completion
    row["cache_hits"] += hits
    row["stream_seconds"] = _seconds(row.get("stream_seconds")) + seconds
    row["errors"] += errors
    row["reported"] += reported
    row["estimated"] += estimated
    row["last_seen"] = time.time()
    if note:
        row["last_error"] = str(note)[:200]


def note(kind: str, *, model: str = "", provider: str = "", session: str = "",
         messages: object = None, prompt: object = None, answer: str = "",
         usage: object = None, seconds: float | None = None,
         error: object = None) -> bool:
    """Record one turn. True when the ledger took it and put it on disk.

    Three kinds, because three things happen to a request: it is answered
    (`"answer"`), it is refused (`"error"`), or it was never sent because the
    economy cache had the answer (`"cache"`). Anything else is a caller's typo and
    is refused quietly — `False`, no row written, no exception.

    A `False` from a real kind means the file would not take the number, and
    nothing more: the turn is still counted in memory and still shows in
    `/stats`, only it will not be there after BeeCode closes, and that is said
    once there rather than written into the loop.

    This function cannot crash a turn. Every failure, including a bug in this
    file, is caught: the user paid for the answer with their patience and is not
    going to lose it because the tally would not write.
    """
    if kind not in KINDS:
        return False
    try:
        return _note(kind, model=model, provider=provider, session=session,
                     messages=messages if prompt is None else prompt,
                     answer=answer, usage=usage, seconds=seconds, error=error)
    except Exception as exc:                  # noqa: BLE001 - a ledger is never a turn
        _NOTICES.append(L(
            f"⚠ the usage ledger skipped a {kind!r} turn ({_describe(exc)}) — the "
            f"answer you got is unaffected",
            f"⚠ учёт пропустил ход {kind!r} ({_describe(exc)}) — на полученный "
            f"ответ это не влияет"))
        return False


def _note(kind: str, *, model: str, provider: str, session: str, messages: object,
          answer: str, usage: object, seconds: object, error: object) -> bool:
    sid = _sid(session)
    data = read()
    key = _key(model, provider)
    row = data["rows"].setdefault(key, _row(str(model or ""), str(provider or "")))
    turn = data["sessions"].setdefault(sid, _row("", ""))
    pair = (row, turn)

    waited = _seconds(seconds) if seconds is not None else 0.0
    if seconds is None:
        started = _TURNS.pop(sid, None)
        waited = _seconds(time.time() - started) if started else 0.0

    if kind == "cache":
        # Nothing was asked, so nothing was spent — but it is worth counting:
        # "31 requests, 12 answered from the cache" is the whole economy mode.
        for target in pair:
            _add(target, hits=1)
    elif kind == "error":
        # A refused request still went out, so it is a request and an error and no
        # tokens: nothing came back to count, and inventing a length for a reply
        # that never arrived is how a ledger stops being believed.
        text = _describe(error) if error is not None else ""
        for target in pair:
            _add(target, requests=1, errors=1, seconds=waited, note=text)
    else:
        from_endpoint = _reported(usage)
        if from_endpoint:
            prompt_tokens, completion_tokens, mark = from_endpoint[0], from_endpoint[1], 1
        else:
            prompt_tokens = _count_prompt(messages, model)
            completion_tokens = count_tokens(answer, model) if answer else 0
            mark = 0
        for target in pair:
            _add(target, requests=1, prompt=prompt_tokens, completion=completion_tokens,
                 seconds=waited, reported=mark, estimated=1 - mark)

    data["version"] = VERSION
    data["updated"] = time.time()
    _trim(data, keep=(key, sid))
    # False means "this is not on disk", not "this did not happen": the numbers
    # for the session are held in memory and still add up until BeeCode closes.
    return _write(data)


# --- reading it back --------------------------------------------------------

def _merge(rows: object) -> dict:
    out = _row("", "")
    for entry in rows or ():
        for counter in COUNTERS:
            out[counter] += (entry.get(counter) or 0)
    return out


def mark_for(row: dict) -> str:
    """`✔` when the endpoint said how many tokens, `~` when we did.

    A row with both is printed as `~`: the honest reading of "some of these
    numbers are ours" is that none of them is a bill.
    """
    reported = _whole(row.get("reported"))
    paid_for = _whole(row.get("requests")) - _whole(row.get("errors"))
    return "✔" if paid_for and reported == paid_for else "~"


def rows() -> list[dict]:
    """Every model this install has asked, biggest first.

    Ordered by requests, then by tokens: the scarce thing about a free model is
    the request, and a person scanning the table is looking for what has been
    spending it.
    """
    entries = list(read()["rows"].values())
    return sorted(entries, key=lambda r: (-_whole(r.get("requests")),
                                          -(_whole(r.get("prompt_tokens"))
                                            + _whole(r.get("completion_tokens"))),
                                          str(r.get("model"))))


def by_provider() -> list[dict]:
    """The same totals, rolled up per endpoint."""
    grouped: dict[str, dict] = {}
    for row in rows():
        name = str(row.get("provider") or "") or "—"
        bucket = grouped.setdefault(name, _row("", name))
        for counter in COUNTERS:
            bucket[counter] += (row.get(counter) or 0)
        if not bucket["last_seen"] or bucket["last_seen"] < _seconds(row.get("last_seen")):
            bucket["last_seen"] = _seconds(row.get("last_seen"))
    return sorted(grouped.values(), key=lambda r: -_whole(r.get("requests")))


def session_row(session: str = "") -> dict | None:
    """What this conversation cost, or None if it has not cost anything yet."""
    return read()["sessions"].get(_sid(session))


def slowest() -> dict | None:
    """The model that kept an answer waiting the longest, in seconds per request.

    Only turns actually timed count: a row that never had a `tick()` in front of
    it has no duration to be wrong about, and reading it as "0 seconds" would
    crown the fastest model in the file.
    """
    best = None
    for row in rows():
        requests = _whole(row.get("requests"))
        waited = _seconds(row.get("stream_seconds"))
        if not requests or not waited:
            continue
        per = waited / requests
        if best is None or per > best["seconds_per_request"]:
            best = {"model": str(row.get("model") or ""),
                    "provider": str(row.get("provider") or ""),
                    "seconds_per_request": per,
                    "requests": requests}
    return best


def last_refusal() -> str:
    """The most recent thing an endpoint refused with, and who refused it."""
    newest = None
    for row in read()["rows"].values():
        if not str(row.get("last_error") or ""):
            continue
        if newest is None or _seconds(row.get("last_seen")) > _seconds(newest[0]):
            newest = (_seconds(row.get("last_seen")), row)
    if newest is None:
        return ""
    row = newest[1]
    return f"{row.get('model') or '?'}{SEP}{row.get('provider') or '?'}: " \
           f"{row['last_error']}"


def snapshot(session: str = "") -> dict:
    """Everything `/stats` prints, in numbers, from one read of the file."""
    data = read()
    every = _merge(data["rows"].values())
    return {
        "all": every,
        "models": rows(),
        "providers": by_provider(),
        "session": session_row(session),
        "conversations": len(data["sessions"]),
        "trimmed": _whole(data.get("trimmed")),
        "slowest": slowest(),
        "refusal": last_refusal(),
        "requests": _whole(every.get("requests")),
        "marks": mark_for(every),
        "saved": PATH.exists(),
    }
