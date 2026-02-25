"""Mutable activity tracker for NATS connection management.

Tracks when the last tool call occurred so background loops can
transition between active and napping states.  Napping reduces
polling frequency but keeps the NATS connection alive for KV
watches (wall changes, session events).

Asyncio is single-threaded, so no locking is needed.
"""

from __future__ import annotations

from datetime import UTC, datetime


class ActivityTracker:
    """Mutable activity state for connection management.

    Companion to the frozen ``ServerState`` — holds mutable timing
    data that background loops read to decide whether to nap.
    """

    __slots__ = ("_last_nap_poll", "_last_tool_call", "_napping")

    def __init__(self) -> None:
        now = datetime.now(UTC)
        self._last_tool_call: datetime = now
        self._last_nap_poll: datetime = now
        self._napping: bool = False

    def touch(self) -> None:
        """Record a tool call.  Clears napping state."""
        self._last_tool_call = datetime.now(UTC)
        self._napping = False

    def enter_nap(self) -> None:
        """Transition to napping state."""
        self._napping = True

    def record_nap_poll(self) -> None:
        """Record that a nap-mode poll just completed."""
        self._last_nap_poll = datetime.now(UTC)

    def idle_seconds(self) -> float:
        """Seconds since the last tool call."""
        return (datetime.now(UTC) - self._last_tool_call).total_seconds()

    def seconds_since_nap_poll(self) -> float:
        """Seconds since the last nap-mode poll."""
        return (datetime.now(UTC) - self._last_nap_poll).total_seconds()

    @property
    def napping(self) -> bool:
        """Whether the server is in napping state."""
        return self._napping
