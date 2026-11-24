"""The channel rate limit: per kind and per span (DES-087)."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Self

from biff.server.channel_content import EventKind

KIND_SECONDS = 30.0
EVENT_LIMIT = 10
SPAN_SECONDS = 600.0


@dataclass(frozen=True, slots=True)
class RateLimit:
    """The limit: *per_kind* seconds between events of one kind, *limit* in *span*."""

    per_kind: float = KIND_SECONDS
    limit: int = EVENT_LIMIT
    span: float = SPAN_SECONDS


class RateWindow:
    """Remember when events were sent, and say when the next one may go.

    The clock is injected so a test can move time without sleeping.
    """

    _limit: RateLimit
    _clock: Callable[[], float]
    _last: dict[EventKind, float]
    _recent: deque[float]

    def __new__(cls, limit: RateLimit, clock: Callable[[], float]) -> Self:
        """Create a window in which nothing has been sent."""
        self = super().__new__(cls)
        self._limit = limit
        self._clock = clock
        self._last = {}
        self._recent = deque()
        return self

    def opens_in(self, kind: EventKind) -> float:
        """Seconds until an event of *kind* may be sent; 0 when it may be now."""
        now = self._clock()
        self._forget(now)
        waits = [0.0]
        if kind in self._last:
            waits.append(self._last[kind] + self._limit.per_kind - now)
        if len(self._recent) >= self._limit.limit:
            waits.append(self._recent[0] + self._limit.span - now)
        return max(waits)

    def record(self, kind: EventKind) -> None:
        """Record that an event of *kind* was sent now."""
        now = self._clock()
        self._last[kind] = now
        self._recent.append(now)

    def _forget(self, now: float) -> None:
        """Drop sends older than the span."""
        while self._recent and self._recent[0] + self._limit.span <= now:
            self._recent.popleft()
