"""Pending-user-input queue.

While the agent is busy the user can keep typing messages. They are parked
here and delivered along with the next model call, so nothing the user typed
is ever lost.
"""
from collections import deque


class PendingQueue:
    def __init__(self):
        self._items = deque()

    def put(self, item: str) -> int:
        """Park a message; returns the current queue length."""
        text = (item or "").strip()
        if text:
            self._items.append(text)
        return len(self._items)

    def drain(self) -> list[str]:
        """Remove and return all pending messages (oldest first)."""
        out = []
        while self._items:
            out.append(self._items.popleft())
        return out

    def __len__(self) -> int:
        return len(self._items)
