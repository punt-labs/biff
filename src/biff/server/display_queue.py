"""Display rotation queue for the status bar.

Manages a rotating queue of display items (wall announcements and talk
messages) for status bar line 2.  Wall items are persistent — they cycle
back into view until they expire or are cleared.  Talk items are ephemeral
— shown once for the display period, then discarded.

The queue is a pure synchronous data structure with no I/O, no asyncio,
and no MCP dependency.  It lives on :class:`~biff.server.state.ServerState`
alongside :class:`~biff.server.activity.ActivityTracker`.

See DES-020 for the notification model that makes this low-latency.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal


@dataclass(frozen=True)
class DisplayItem:
    """A single item in the status bar rotation queue.

    ``kind`` determines lifecycle:
    - ``"wall"`` — persistent; cycles back after its display turn
    - ``"talk"`` — ephemeral; shown once then discarded

    ``text`` is the rendered display string, ready for the status bar.
    ``source_key`` prevents duplicates (idempotent ``add``).
    ``added_at`` records when this item entered the queue.
    """

    kind: Literal["wall", "talk"]
    text: str
    source_key: str
    added_at: datetime = field(default_factory=lambda: datetime.now(UTC))


type ClockFn = Callable[[], float]


class DisplayQueue:
    """Rotation queue for status bar line 2 display items.

    Wall items cycle indefinitely (removed only when their ``WallPost``
    expires or is cleared).  Talk items are shown once then dropped.

    All methods are synchronous — asyncio is single-threaded so no
    locking is needed.  The queue is driven by the poller's 2s tick
    calling :meth:`advance_if_due`.

    *clock* defaults to :func:`time.monotonic` but can be injected
    for deterministic testing (no ``time.sleep`` needed).
    """

    __slots__ = ("_clock", "_current_index", "_items", "_slot_start", "_turn_duration")

    def __init__(
        self,
        turn_duration: float = 15.0,
        *,
        clock: ClockFn = time.monotonic,
    ) -> None:
        self._items: list[DisplayItem] = []
        self._current_index: int = 0
        self._clock: ClockFn = clock
        self._slot_start: float = clock()
        self._turn_duration: float = turn_duration

    def add(self, item: DisplayItem) -> bool:
        """Add an item if its ``source_key`` is not already present.

        Returns ``True`` if the item was added (new), ``False`` if
        a duplicate was found.  When this is the first item, resets
        the slot timer so the new item gets its full display period.
        """
        for existing in self._items:
            if existing.source_key == item.source_key:
                return False
        was_empty = len(self._items) == 0
        self._items.append(item)
        if was_empty:
            self._current_index = 0
            self._slot_start = self._clock()
        return True

    def remove_by_source_key(self, source_key: str) -> bool:
        """Remove the item with the given ``source_key``.

        Returns ``True`` if found and removed.  Adjusts the current
        index to maintain the rotation invariant.
        """
        for i, item in enumerate(self._items):
            if item.source_key == source_key:
                self._remove_at(i)
                return True
        return False

    def remove_by_kind(self, kind: Literal["wall", "talk"]) -> None:
        """Remove all items of the given kind.

        Used for ``talk_end`` (clear all talk) and ``wall clear``
        (clear all wall items).  Resets the slot timer so the
        newly-current item gets a full display period.
        """
        i = 0
        while i < len(self._items):
            if self._items[i].kind == kind:
                self._remove_at(i)
            else:
                i += 1
        if self._items:
            self._slot_start = self._clock()

    def current(self) -> DisplayItem | None:
        """Return the item currently in the display slot.

        Returns ``None`` when the queue is empty.
        """
        if not self._items:
            return None
        return self._items[self._current_index]

    def advance_if_due(self) -> bool:
        """Advance rotation if the current slot has exceeded its turn.

        Talk items are discarded after their turn.  Wall items remain
        in the rotation; when there are multiple items, the cursor
        advances to the next one.  With a single wall item, it stays
        selected but its slot timer is still reset.

        Returns ``True`` if the slot timer expired and the queue state
        (timer and/or cursor) was advanced.  The displayed item may or
        may not have changed (for example, with a single wall item).
        Returns ``False`` if the slot timer has not yet expired or the
        queue is empty (no advancement performed).
        """
        if not self._items:
            return False
        elapsed = self._clock() - self._slot_start
        if elapsed < self._turn_duration:
            return False

        current = self._items[self._current_index]
        if current.kind == "talk":
            # Ephemeral — discard after one display turn
            self._remove_at(self._current_index)
        else:
            # Persistent — advance cursor past this item
            if len(self._items) > 1:
                self._current_index = (self._current_index + 1) % len(self._items)
            # Single wall item: cursor stays at 0, but we still reset
            # the slot timer so the description gets a fresh "expires in"

        self._slot_start = self._clock()
        return True

    def force_to_front(self, source_key: str) -> bool:
        """Move the item with ``source_key`` to the current display slot.

        Used when a new talk message arrives — it should be shown
        immediately rather than waiting for the current item to
        finish its turn.

        Returns ``True`` if found and moved, ``False`` if not found.
        """
        for i, item in enumerate(self._items):
            if item.source_key == source_key:
                self._current_index = i
                self._slot_start = self._clock()
                return True
        return False

    def snapshot(self) -> list[DisplayItem]:
        """Return a shallow copy of the queue for inspection/testing."""
        return list(self._items)

    def _remove_at(self, index: int) -> None:
        """Remove the item at *index* and fix the current index invariant.

        After removal:
        - If queue is empty, index resets to 0.
        - If the removed item was before the cursor, decrement cursor.
        - If the cursor is now past the end, wrap to 0.
        """
        self._items.pop(index)
        if not self._items:
            self._current_index = 0
            return
        if index < self._current_index:
            self._current_index -= 1
        if self._current_index >= len(self._items):
            self._current_index = 0
