"""Mutable activity tracker for lazy NATS connection management.

Tracks when the last tool call occurred so background loops can
transition between active and napping (POP-mode) states.  Napping
releases the TCP connection; the next tool call wakes back to active.

Asyncio is single-threaded, so no locking is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime


class ActivityTracker:
    """Mutable activity state for lazy connection management.

    Companion to the frozen ``ServerState`` — holds mutable timing
    data that background loops read to decide whether to nap.
    """

    __slots__ = ("_last_pop", "_last_tool_call", "_napping")

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self._last_tool_call: datetime = now
        self._last_pop: datetime = now
        self._napping: bool = False

    def touch(self) -> None:
        """Record a tool call.  Clears napping state."""
        self._last_tool_call = datetime.now(UTC)
        self._napping = False

    def enter_nap(self) -> None:
        """Transition to napping (POP-mode) state."""
        self._napping = True

    def record_pop(self) -> None:
        """Record that a POP fetch just completed."""
        self._last_pop = datetime.now(UTC)

    def idle_seconds(self) -> float:
        """Seconds since the last tool call."""
        return (datetime.now(UTC) - self._last_tool_call).total_seconds()

    def seconds_since_pop(self) -> float:
        """Seconds since the last POP fetch."""
        return (datetime.now(UTC) - self._last_pop).total_seconds()

    @property
    def napping(self) -> bool:
        """Whether the server is in napping (POP-mode) state."""
        return self._napping
