"""Sessions that outlive the process: checkpoints, crash recovery, a bounded pile.

Before this file a conversation reached the disk in exactly two places — the
`finally` of the REPL loop and an explicit `/save`. Both are run by a process that
has to survive until the end of the session. A window closed on a phone over
Termux, a crash in the middle of a turn, a laptop that slept through an answer:
the plan the agent wrote and every tool result the user paid quota for went with
it, and nothing said so.

Four rules, in the order the audit asked for them:

1. **Checkpoint after each completed turn.** Not per token — an fsync per token on
   phone flash is its own kind of outage — but often enough that the most a crash
   can take is the turn in flight. Turns arriving faster than `DEBOUNCE_SECONDS`
   coalesce into one write, and the coalesced state is written by the first
   checkpoint after the window, so the debounce is bounded by the window and never
   drops a state permanently.
2. **Say something on start.** A session file whose last writer was a checkpoint
   (`closed: false`) is a session that never finished. The next start reports it in
   one line, and `--continue` / `/continue` opens it. Silence was the bug: a user
   who only notices that the history looks short has already lost the session.
3. **A failed checkpoint is not the end of the conversation, and it is not a save.**
   Disk full, folder gone, a file held by antivirus on Windows: warn once, with the
   reason, and keep answering. The final clean save says out loud when it did not
   land, so "saved" is never a word attached to bytes that are not on disk.
4. **The pile is bounded, and its removals are announced.** Session files are
   capped by count and by age; the file of the session that is open right now is
   never a candidate, and neither is a file whose stamp lies about being in the
   future (a clock that jumped backwards). What was deleted is named, one line
   each — a deletion this project has not said out loud has been called a bug
   before. The pre-`/compact` backups that live in the same folder are not session
   files and are never touched.

The expensive bytes: a transcript carries every tool result verbatim, and the
model can be handed those bytes again later out of the user's context window.
Verbatim is the default, because a checkpoint that quietly rewrites history is
worse than no checkpoint. `BEECODE_AUTOSAVE_TRIM` (or `trim=`) trades tool bodies
over the limit on disk for a smaller file, keeping the `[tool result] tool=…`
header and the size, so the turn stays attributable and can be re-run. In-memory
state keeps every byte either way.

Nothing here sleeps, nothing here leaves the machine, and nothing here raises out
of `checkpoint()`: the caller is the loop that is answering the user.

"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from beeagent.i18n import L
from beeagent.core.session import (
    EXECUTED_RESULT, Session, frame_as_data, is_framed, read_head, session_files,
    unframe,
)

# --- the bounds, all of them in one place -------------------------------------

# A turn finishing inside this window does not get its own write. The first turn of
# any real task takes seconds of model time, so the window only bites a burst of
# cheap steps — and a burst of cheap steps is exactly the fsync storm it prevents.
DEBOUNCE_SECONDS = 3.0

# The window stretches to stay ahead of the cost of the last write, up to this.
# Measured on a 200-message session (see the note on `_window()`): a checkpoint of
# a 0.5 MB transcript is ~17 ms, of a 2 MB one ~27 ms — so a long conversation
# throttles itself to roughly one write per second of writing, per turn it is asked
# for, instead of paying full price for every step of a 50-tool-call task.
MAX_DEBOUNCE_SECONDS = 15.0
WRITE_COST_FACTOR = 3.0

# How many files are kept, and how stale a kept file may get. Both deliberately
# generous: the ceiling exists for the phone with 40 MB free, not for the person
# who wants every conversation back from last month. Below both bounds a session
# file is left alone forever.
MAX_SESSIONS = 50
MAX_AGE_DAYS = 30

# A tool body over this many bytes is not written out when trimming is on.
TOOL_BODY_LIMIT = 8192

# Opt-in, environment-shaped: the config model ignores unknown keys and drops them
# on the next save, so a config field is not this file's call to make. Adding
# `autosave_trim: bool = False` to `config/schema.py` is the one-line follow-up.
TRIM_ENV = "BEECODE_AUTOSAVE_TRIM"

# Which unclean sessions have already been announced. Kept outside the sessions
# folder so no session glob can mistake it for a transcript.
ACK_NAME = "autosave.json"

DAY_SECONDS = 86400.0

# Everything a write can raise when the disk said no: a full volume, a folder
# deleted underneath us, a name Windows holds open. Caught, always — the tenth
# failure still must not end the conversation.
WRITE_FAILURES = (OSError, ValueError, TypeError, UnicodeEncodeError)

# Failures a read of somebody else's file can raise, including one that is not a
# session at all.
READ_FAILURES = (OSError, ValueError, TypeError, KeyError, UnicodeDecodeError)


def sessions_dir(workdir: str = ".") -> Path:
    return Path(workdir) / ".beeagent" / "sessions"


def trim_setting(value=None, default_limit: int = TOOL_BODY_LIMIT) -> int:
    """The byte limit trimming should use, or 0 for off.

    `BEECODE_AUTOSAVE_TRIM` reads as a switch or as a size: `1` trims at the
    default, `20000` trims at 20 kB — where `1` meaning "one byte" would be a
    switch nobody writes and a transcript nobody wants. A value that is neither a
    recognisable "off" nor a number means the default, because a typo in an opt-in
    should mean the opt-in happened, not that it quietly did not.
    """
    raw = value if value is not None else os.environ.get(TRIM_ENV, "")
    if isinstance(raw, bool):
        return default_limit if raw else 0
    text = str(raw).strip().lower()
    if text in ("", "0", "off", "no", "false", "none", "never"):
        return 0
    if text in ("1", "true", "yes", "y", "on"):
        return default_limit
    try:
        number = int(text)
    except ValueError:
        return default_limit
    if number > 1:
        return number
    if number == 1:
        return default_limit
    return 0                              # zero or a negative: a switch read as off


# --- the shape of one tool result ---------------------------------------------

TRIMMED_NOTE = ("[autosave] {size} bytes of this tool output were left out of the "
                "saved copy to keep the checkpoint small. The call it answers is the "
                "line above; run it again for the whole text.")


def _reference(body: str) -> str:
    """What replaces a body that was too big to keep.

    Model-facing, so English only, exactly like the `[format note]` the loop
    appends to a repaired call: the transcript is read back by a model, and a file
    that switched language halfway is worse for it than for anyone. The header line
    above still names the tool and whether it errored — that is the reference the
    user needs in order to re-run it.
    """
    return f"\n[{TRIMMED_NOTE.format(size=len(body.encode('utf-8')))}]"


def trim_tool_bodies(rows: list[dict], limit: int) -> tuple[list[dict], int]:
    """The transcript with oversized tool bodies folded into a reference.

    Only *executed* results are folded — the `[tool result] tool=name …` shape the
    loop builds. BeeCode's own refusals share the prefix and are short by nature,
    and user or assistant text is never touched: what gets folded here is what the
    model caused to be printed, not what anybody said.
    """
    if limit <= 0:
        return rows, 0
    trimmed = 0
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        new = dict(row)
        for field in ("content", "tool_result"):
            text = new.get(field)
            if not isinstance(text, str) or not text.strip():
                continue
            shortened = _trim_one(text, limit)
            if shortened is not None:
                new[field] = shortened
                trimmed += 1
        out.append(new)
    return out, trimmed


def _trim_one(text: str, limit: int) -> str | None:
    """The shortened body, or None when this text stays exactly as it is."""
    framed = is_framed(text)
    body = unframe(text) if framed else text
    if not EXECUTED_RESULT.match(body.strip()):
        return None
    head, separator, rest = body.partition("\n")
    if not separator or len(rest.encode("utf-8")) <= limit:
        return None
    # Keep the first line: a tool that printed "3 files changed" and then a wall of
    # diff still leaves something the model can quote without inventing.
    first_line, line_break, _ = rest.partition("\n")
    kept = first_line if line_break and len(first_line.encode("utf-8")) <= limit else ""
    shortened = f"{head}{separator}{kept}{_reference(rest)}"
    return frame_as_data(shortened) if framed else shortened


# --- what one checkpoint answers ----------------------------------------------

class Checkpoint:
    """The outcome of one `checkpoint()` / `close()`. Never raised, always said."""

    __slots__ = ("action", "reason", "path", "messages")

    def __init__(self, action: str, reason: str = "", path: str = "",
                 messages: int = 0):
        self.action = action          # written | coalesced | skipped | failed
        self.reason = reason
        self.path = path
        self.messages = messages

    @property
    def saved(self) -> bool:
        """Only ever True when the bytes are on disk under this exact name."""
        return self.action == "written"

    def __repr__(self) -> str:
        return f"Checkpoint({self.action!r}, {self.reason!r})"


class Unclean:
    """A session file whose last writer was a checkpoint."""

    __slots__ = ("session_id", "path", "messages", "saved_at")

    def __init__(self, session_id: str, path: str, messages: int, saved_at: float):
        self.session_id = session_id
        self.path = path
        self.messages = messages
        self.saved_at = saved_at

    def __repr__(self) -> str:
        return f"Unclean({self.session_id!r}, {self.messages} messages)"


# --- the saver ----------------------------------------------------------------

class AutoSaver:
    """Checkpoint a session as it grows, then close it honestly.

    One instance belongs to one REPL run. `workdir` is where the sessions folder
    lives and `on_message` is how the user hears about it — the REPL prints through
    the same dim one-line notes it uses for everything else. `monotonic` and `now`
    are injectable because the clock is not trustworthy: a phone that synced its
    time mid-conversation moves `now` by minutes, and a test that wants to prove
    the debounce must not have to wait out a real window to do it.
    """

    def __init__(self, workdir: str = ".", session: Session = None, *,
                 debounce_seconds: float = DEBOUNCE_SECONDS,
                 max_sessions: int = MAX_SESSIONS, max_age_days: int = MAX_AGE_DAYS,
                 trim=None, tool_body_limit: int = TOOL_BODY_LIMIT,
                 on_message=None, now=time.time, monotonic=time.monotonic):
        self.workdir = str(workdir or ".")
        self.debounce_seconds = max(0.0, float(debounce_seconds))
        self.max_sessions = max(1, int(max_sessions))
        self.max_age_days = max(0, int(max_age_days))
        self.tool_body_limit = max(1, int(tool_body_limit))
        # `trim=None` means "ask the environment"; anything else is an explicit
        # switch or size, and wins over it.
        self.trim_limit = trim_setting(trim, self.tool_body_limit)
        self.on_message = on_message
        self._now = now
        self._monotonic = monotonic

        self.session: Session | None = None
        self.session_source = None
        self.adopt(session)

        # bookkeeping a test (and a status line) reads instead of guessing
        self.writes = 0
        self.coalesced = 0
        self.failures = 0
        self.trimmed_rows = 0
        self.turns = 0
        self.warning = ""
        self.last: Checkpoint | None = None
        self.removed: list[tuple[str, str]] = []
        self.closed = False
        self._dirty = False
        self._pending_since = None
        self._last_attempt = None
        self.last_write_seconds = 0.0

    # --- what the REPL can change at runtime ---------------------------------

    def _window(self) -> float:
        """How long between writes, stretched by what the last one cost.

        The floor is the configured debounce. A checkpoint rewrites the whole
        transcript, so its price is the size of the conversation, and a session of
        a few hundred messages on slow flash is the case where writing every turn
        would be the problem it is meant to solve. Measured here rather than
        assumed: the number used is the wall time of the previous write times
        `WRITE_COST_FACTOR`, capped, so the throttle tightens on the machine that
        needs it and never fires on one that does not.

        A `debounce_seconds` of 0 means "write every turn", and nothing stretches
        a promise that explicit: the cost factor exists to bound work nobody asked
        for, not to overrule a caller who asked for none.
        """
        if not self.debounce_seconds:
            return 0.0
        stretched = self.last_write_seconds * WRITE_COST_FACTOR
        if not stretched:
            return self.debounce_seconds
        return min(max(self.debounce_seconds, stretched), MAX_DEBOUNCE_SECONDS)

    def attach(self, session_source) -> None:
        """Follow the live session: `/continue` and `/reset` swap the object under us."""
        self.session_source = session_source

    def set_trim(self, enabled) -> int:
        """Turn tool-body trimming on or off (`1` / `0` / a byte count)."""
        self.trim_limit = trim_setting(enabled, self.tool_body_limit)
        return self.trim_limit

    def report(self, text: str) -> None:
        """Say one line to the user, if anyone is listening."""
        if self.on_message is None or not text:
            return
        try:
            self.on_message(text)
        except Exception:
            pass                      # a UI that cannot print is no reason to stop

    # --- rule 1: the checkpoint ----------------------------------------------

    def adopt(self, session: Session | None) -> None:
        """Start following another session, and close the one we were following.

        The session we were checkpointing is finished with — the user typed
        `/reset` or `/continue`, or this run is over — and the transcript last
        written is as complete as it will ever be. Marking it closed here is what
        keeps an abandoned session from being reported as a crash on the next
        start, which would train the user to ignore the one line that matters.
        """
        previous = self.session
        if session is previous:
            return
        if previous is not None and not self.closed:
            self._write(previous, clean=True, checkpoint=False, quiet=True)
        self.session = session
        self._dirty = False
        self._pending_since = None

    def current(self) -> Session | None:
        """The session to checkpoint, following the REPL if it gave us a getter."""
        if self.session_source is not None:
            try:
                self.adopt(self.session_source())
            except Exception:
                return self.session
        return self.session

    def checkpoint(self, session: Session = None, reason: str = "") -> Checkpoint:
        """Called after a completed turn. Cannot raise; will not take two writes.

        The window is measured on `monotonic()`, never on the wall clock: a clock
        that jumps backwards mid-session would otherwise make every later turn read
        as "still inside the window", and the failure mode of that is silent
        amnesia — the exact thing this file exists to prevent.
        """
        if self.closed:
            return Checkpoint("skipped", "this session is already closed")
        session = session if session is not None else self.current()
        if session is None:
            return Checkpoint("skipped", "no session yet")
        self.adopt(session)
        self.turns += 1
        tick = float(self._monotonic())
        window = self._window()
        inside = (window and self._last_attempt is not None
                  and (tick - self._last_attempt) < window)
        if inside and not self._pending_is_stale(tick, window):
            self._dirty = True
            if self._pending_since is None:
                self._pending_since = tick
            self.coalesced += 1
            self.last = Checkpoint("coalesced", f"inside the {window:g}s window")
            return self.last
        # The window is measured from the end of the last write: a checkpoint that
        # took two seconds on slow flash should not spend the next window being
        # "inside" the one it has already paid for.
        result = self._write(session, clean=False, checkpoint=True)
        self._last_attempt = float(self._monotonic())
        if result.saved:
            self._dirty = False
            self._pending_since = None
        else:
            # Keep the pending flag: the next window retries rather than leaving a
            # turn that failed to land permanently unwritten.
            self._dirty = True
            if self._pending_since is None:
                self._pending_since = tick
        self.last = result
        return result

    def _pending_is_stale(self, tick: float, window: float = None) -> bool:
        """Write a coalesced state once it has outlived the window, burst or not.

        Without this, a session whose turns all land inside one window would
        checkpoint once at the start and then never again while the burst lasted.
        """
        window = self._window() if window is None else window
        return (self._dirty and self._pending_since is not None
                and window and (tick - self._pending_since) >= window)

    def flush(self, reason: str = "") -> Checkpoint:
        """Write what is pending right now — the call for an idle prompt."""
        if self.closed:
            return Checkpoint("skipped", "this session is already closed")
        session = self.current()
        if session is None:
            return Checkpoint("skipped", "no session yet")
        if not self._dirty:
            return Checkpoint("skipped", "nothing new since the last checkpoint")
        self.last = self._write(session, clean=False, checkpoint=True)
        self._last_attempt = float(self._monotonic())
        if self.last.saved:
            self._dirty = False
            self._pending_since = None
        return self.last

    def pending(self) -> bool:
        """True while memory holds a turn the disk does not."""
        return bool(self._dirty)

    # --- rules 3 and 4: the honest end ---------------------------------------

    def close(self, session: Session = None) -> Checkpoint:
        """The clean save, then the sweep. Answers whether the save landed.

        Order matters: the transcript first, then what was pruned, so a user who
        only reads the last line still has their session on disk. A failed final
        save is reported every time rather than once — what it means is that the
        conversation is about to be gone, and that cannot be said too loudly.
        """
        session = session if session is not None else self.current()
        self.closed = True
        result = Checkpoint("skipped", "no session to save")
        if session is not None:
            result = self._write(session, clean=True, checkpoint=False)
            if not result.saved:
                self.report(L(f"the session was NOT saved ({result.reason}) — closing "
                              f"this window loses {len(session.messages)} message(s)",
                              f"сессия НЕ сохранена ({result.reason}) — закрытие окна "
                              f"потеряет {len(session.messages)} сообщ(ий)"))
        self.last = result
        try:
            for line in self.prune(getattr(session, "session_id", None)):
                self.report(line)
        except Exception as e:                    # a sweep that cannot run is not a
            self.report(L(f"old sessions were not checked ({e})",   # reason to lose
                          f"старые сессии не проверены ({e})"))     # this one either
        return result

    def prune(self, active_id: str = None) -> list[str]:
        """Cut the pile back inside the bounds, and name everything that came out."""
        removed = prune_sessions(self.workdir, max_sessions=self.max_sessions,
                                 max_age_days=self.max_age_days, active_id=active_id,
                                 now=self._now)
        self.removed.extend(removed)
        return [pruned_line(session_id, why, self.workdir)
                for session_id, why in removed]

    # --- the write itself ----------------------------------------------------

    def _prepare(self, rows: list[dict]) -> list[dict]:
        if not self.trim_limit:
            return rows
        trimmed, count = trim_tool_bodies(rows, self.trim_limit)
        if count:
            self.trimmed_rows += count
        return trimmed

    def _write(self, session: Session, clean: bool, checkpoint: bool,
               quiet: bool = False) -> Checkpoint:
        started = float(self._monotonic())
        try:
            path = session.save(self.workdir, clean=clean, prepare=self._prepare,
                                checkpoint=checkpoint, stamp=float(self._now()))
        except WRITE_FAILURES as e:
            return self._failed(session, e, quiet)
        except Exception as e:                   # a save path nobody predicted
            return self._failed(session, e, quiet)
        # Only a write that happened sets the pace for the next one: a disk that
        # refused this second must not be allowed to decide how often we ask.
        self.last_write_seconds = max(0.0, float(self._monotonic()) - started)
        self.writes += 1
        return Checkpoint("written", "", str(path), len(session.messages))

    def _failed(self, session: Session, exc: Exception, quiet: bool) -> Checkpoint:
        detail = f"{type(exc).__name__}: {exc}"
        self.failures += 1
        if quiet:
            # A handoff write (an abandoned session being marked closed) is not
            # worth interrupting the user for, but it is worth remembering.
            self.warning = self.warning or detail
        else:
            self._warn_once(detail)
        return Checkpoint("failed", detail, messages=len(session.messages))

    def _warn_once(self, reason: str) -> None:
        """Once per session, with the reason, and never in the shape of an error.

        A checkpoint that cannot be written is no reason to stop answering the
        user. It is a reason to tell them once that the net is out — in the same
        breath, what the net holds and how to make it cheaper.
        """
        if self.warning:
            return
        self.warning = reason
        hint = ""
        if not self.trim_limit:
            hint = L(f" — {TRIM_ENV}=1 also drops saved tool bodies over "
                     f"{self.tool_body_limit} bytes and keeps the reference "
                     f"(off by default)",
                     f" — {TRIM_ENV}=1 при сохранении убирает тела инструментов больше "
                     f"{self.tool_body_limit} байт, оставляя ссылку (по умолчанию выключено)")
        self.report(L(f"⚠ autosave cannot write its checkpoint ({reason}) — nothing is "
                      f"lost yet and the conversation goes on, but a crash now takes "
                      f"every turn since the last checkpoint{hint}",
                      f"⚠ автосохранение не может записать чекпоинт ({reason}) — пока "
                      f"ничего не потеряно и разговор продолжается, но сбой заберёт все "
                      f"шаги с последнего чекпоинта{hint}"))

    def status(self) -> dict:
        """What an `/autosave` line would print, and what the tests assert."""
        return {
            "workdir": self.workdir,
            "session": getattr(self.session, "session_id", ""),
            "writes": self.writes,
            "coalesced": self.coalesced,
            "failures": self.failures,
            "pending": self.pending(),
            "debounce_seconds": self.debounce_seconds,
            "window_seconds": round(self._window(), 4),
            "last_write_ms": round(self.last_write_seconds * 1000, 2),
            "trim_bytes": self.trim_limit,
            "trimmed_messages": self.trimmed_rows,
            "warning": self.warning,
            "max_sessions": self.max_sessions,
            "max_age_days": self.max_age_days,
            "removed": list(self.removed),
        }


# --- rule 2: what a start says -------------------------------------------------

def _entries(workdir: str = ".") -> list[tuple[float, str, int, bool]]:
    """`(stamp, session id, message count, unclean?)` for every session file.

    One walk of the folder, because both the recovery notice and the tests for it
    have to agree on what is there. A file that cannot be read contributes nothing:
    nothing that cannot be read is ever deleted or reported either.
    """
    found = []
    for path in session_files(sessions_dir(workdir)):
        head = read_head(path)
        if not head:
            continue
        stamp = head.get("saved_at")
        stamp = float(stamp) if stamp is not None else _mtime(path)
        count = head.get("message_count")
        if count is None:                       # written before this code existed
            count = message_count(path)
        found.append((stamp, path.stem, int(count), head.get("closed") is False))
    found.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return found


def find_unclean(workdir: str = ".") -> list[Unclean]:
    """Sessions that never reported themselves finished, newest first."""
    directory = sessions_dir(workdir)
    return [Unclean(session_id, str(directory / f"{session_id}.json"), count, stamp)
            for stamp, session_id, count, unclean in _entries(workdir) if unclean]


def message_count(path) -> int:
    """How many messages a session file holds, for a legacy file without a header count."""
    try:
        data = json.loads(Path(str(path)).read_text(encoding="utf-8"))
    except READ_FAILURES:
        return 0
    rows = data.get("messages") if isinstance(data, dict) else None
    return len(rows) if isinstance(rows, list) else 0


def recovered_notice(workdir: str = ".", current_id: str = None,
                     acknowledge: bool = True) -> str:
    """The one line a start owes the user after an unclean end, or "" for nothing to say.

    Acknowledged as it is said, so the same loss is not announced on every launch:
    once told, the transcript is what it is. A start that loads the recovered
    session (`--continue`) has nothing to report — that is the fix, not the notice.
    """
    unclean = [item for item in _entries(workdir)
               if item[3] and item[1] != current_id]
    if not unclean:
        return ""
    reported = _read_ack(workdir)
    unheard = [item for item in unclean if item[1] not in reported]
    if not unheard:
        return ""
    if acknowledge:
        _write_ack(workdir, reported | {item[1] for item in unclean})
    _stamp, session_id, count = unheard[0][:3]
    extra = ""
    if len(unheard) > 1:
        extra = L(f" (+{len(unheard) - 1} older session(s) never finished either, "
                  f"/sessions)",
                  f" (и ещё {len(unheard) - 1} незавершённых, /sessions)")
    return L(f"last session ended uncleanly, recovered {count} messages, "
             f"/continue opens it ({session_id}){extra}",
             f"прошлая сессия завершилась некорректно, восстановлено {count} "
             f"сообщ(ий), откроет её /continue ({session_id}){extra}")


def recovered_session(workdir: str = ".") -> Session | None:
    """The session `--continue` should open: an unfinished one beats a finished one.

    A crash is the thing the user most wants back. `list_sessions()[-1]` happens to
    agree today because ids embed a creation time; this is the rule rather than the
    coincidence, and it survives the clock moving under it.
    """
    for entry in find_unclean(workdir):
        try:
            return Session.load(entry.session_id, workdir)
        except READ_FAILURES:
            continue
    return None


# --- rule 4: the bounded pile ---------------------------------------------------

def _mtime(path) -> float:
    try:
        return os.path.getmtime(str(path))
    except OSError:
        return 0.0


def prune_sessions(workdir: str = ".", max_sessions: int = MAX_SESSIONS,
                   max_age_days: int = MAX_AGE_DAYS, active_id: str = None,
                   now=None) -> list[tuple[str, str]]:
    """Delete what is past the age or over the cap; answer `[(id, why)]` for each.

    Two bounds, both conservative in the same direction — keep:

    * **age**: `clock - stamp` has to be both non-negative and past the horizon. A
      stamp ahead of the clock means the clock moved, and deleting a file the clock
      calls the future is how the day-rollover bug in this project already behaved
      once. `max_age_days=0` switches the age bound off.
    * **count**: the newest `max_sessions` by stamp survive. The session open right
      now is never a candidate whatever its stamp says — pruning the transcript the
      user is writing is the bug this whole function exists not to have — but it
      still spends one of the slots, because a file on disk is a file on disk.

    Files this cannot recognise as BeeCode sessions (a `/compact` backup, anything
    a person or another program dropped in the folder) are not candidates at all.
    """
    clock = float((now or time.time)())
    directory = sessions_dir(workdir)
    if not directory.is_dir():
        return []
    horizon = max_age_days * DAY_SECONDS if max_age_days > 0 else 0.0
    scored: list[tuple[float, str]] = []
    removed: list[tuple[str, str]] = []
    active_on_disk = False
    for path in session_files(directory):
        head = read_head(path)
        if not head:
            continue                             # unreadable, or not ours: never delete
        stem = path.stem
        stamp = head.get("saved_at")
        stamp = float(stamp) if stamp is not None else _mtime(path)
        if stem == active_id:
            # Not a removal candidate at any age, but a file on disk all the same,
            # so it spends one slot of the cap. A conversation that is being written
            # now cannot also be free.
            active_on_disk = True
            continue
        if horizon and (clock - stamp) > horizon and (clock - stamp) >= 0.0:
            if _remove(path):
                removed.append((stem, f"older than {max_age_days} days"))
            continue
        scored.append((stamp, stem))

    # The cap last, so an age sweep cannot leave the folder looking small enough to
    # skip a check both bounds say is still needed.
    budget = max_sessions - (1 if active_on_disk else 0)
    if max_sessions and len(scored) > max(budget, 0):
        ordered = sorted(scored, key=lambda item: (item[0], item[1]), reverse=True)
        doomed = ordered[max(budget, 0):]
        # Reported oldest-first, so what the user reads walks the folder in the
        # order the files were made instead of in the order the sort happened to
        # leave them.
        for _stamp, stem in sorted(doomed, key=lambda item: (item[0], item[1])):
            if _remove(directory / f"{stem}.json"):
                removed.append((stem, f"past the {max_sessions}-session cap"))
    return removed


def _remove(path) -> bool:
    try:
        os.remove(str(path))
    except OSError:
        return False
    return True


def pruned_line(session_id: str, why: str, workdir: str = ".") -> str:
    """The announcement one removal is worth."""
    gone = sessions_dir(workdir) / f"{session_id}.json"
    return L(f"🗑 removed old session {session_id} ({why}) — deleted {gone}",
             f"🗑 удалена старая сессия {session_id} ({why}) — файл {gone}")


# --- the acknowledgement ledger --------------------------------------------------

def _ack_path(workdir: str = ".") -> Path:
    return Path(workdir) / ".beeagent" / ACK_NAME


def _read_ack(workdir: str = ".") -> set:
    try:
        data = json.loads(_ack_path(workdir).read_text(encoding="utf-8"))
    except READ_FAILURES:
        return set()
    ids = data.get("reported") if isinstance(data, dict) else None
    return {str(item) for item in ids} if isinstance(ids, list) else set()


def _write_ack(workdir: str = ".", ids=None) -> None:
    """Keep only ids that still have a file, so the ledger cannot become the pile."""
    live = {path.stem for path in session_files(sessions_dir(workdir))}
    wanted = sorted({str(item) for item in (ids or []) if str(item) in live})[-MAX_SESSIONS:]
    path = _ack_path(workdir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"reported": wanted}, indent=2, ensure_ascii=False),
                        encoding="utf-8")
    except OSError:
        pass                      # a ledger that cannot be written re-tells the story
                                  # on the next start — the lesser of the two lies
