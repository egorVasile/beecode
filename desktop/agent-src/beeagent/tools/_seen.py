"""What the model was last shown, and the exact bytes it was taken from.

The agent works from a copy of a file that sits in the conversation. When
something else saves that file — the user's editor, another BeeCode on the same
checkout — the copy in the conversation is stale, and an edit applied to it
silently drops the other writer's work. A stamp taken at read time is the only
cheap way to notice.
"""
from __future__ import annotations

STAMPS: dict[str, tuple] = {}


def stamp_of(path) -> tuple:
    try:
        info = path.stat()
    except OSError:
        return ()
    return (info.st_mtime_ns, info.st_size, getattr(info, "st_ino", 0))


def remember(path) -> None:
    STAMPS[str(path)] = stamp_of(path)


def changed_since_read(path) -> bool:
    """True when we showed the model this file and it is no longer those bytes."""
    key = str(path)
    if key not in STAMPS:
        return False
    return STAMPS[key] != stamp_of(path)


def forget(path) -> None:
    STAMPS.pop(str(path), None)
