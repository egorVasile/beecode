"""One door for questions the agent asks the human mid-turn.

Both the terminal and the desktop need the same thing: announce a question,
then block until the human answers. The terminal reads `input()` on a worker
thread; the desktop answers over HTTP. This module is the shared registry —
`ask()` announces through the callback and waits on a threading event,
`answer()` (called by whatever UI is listening) releases it.

Threading, not asyncio: `ask()` runs inside `asyncio.to_thread` (tools are
sync), so it must not touch the event loop. A captured request cannot be
resent and dies with the timestamp — the same reasoning as the pool seat.
"""
from __future__ import annotations

import secrets
import threading
import time

_pending: dict[str, dict] = {}
_seq = 0
_lock = threading.Lock()


def ask(callback, question: str, meta: dict | None = None) -> str:
    """Announce `question` and block until `answer()` arrives.

    `meta` rides along so the UI can tell a path-grant question (three
    buttons) from a free-form one (a text field). The answer is whatever the
    human chose; the caller maps it to its own vocabulary.
    """
    global _seq
    with _lock:
        _seq += 1
        ask_id = f"ask-{_seq}-{int(time.time())}-{secrets.token_hex(4)}"
        record = {"event": threading.Event(), "question": question,
                  "meta": meta or {}, "answer": ""}
        _pending[ask_id] = record
    if callable(callback):
        callback("ask", {"id": ask_id, "question": question, "meta": meta or {}})
    record["event"].wait()
    with _lock:
        _pending.pop(ask_id, None)
    return record["answer"]


def answer(ask_id: str, text: str) -> bool:
    """Release a pending question. False when it is unknown or already answered."""
    with _lock:
        record = _pending.get(ask_id)
        if record is None:
            return False
        record["answer"] = str(text or "(no answer)")
    record["event"].set()
    return True


def pending() -> list[dict]:
    """What is waiting right now — for a UI that wants to list open questions."""
    with _lock:
        out = []
        for ask_id, record in _pending.items():
            out.append({"id": ask_id, "question": record["question"],
                        "meta": record["meta"]})
        return out
