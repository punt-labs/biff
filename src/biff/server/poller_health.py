"""Whether the poller is keeping up: its deaths in a row and the user's notice."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PollerHealth:
    """The poller's supervisor writes it; the description renderer reads it.

    The notice that notifications stopped is state, not a one-off edit of a
    tool description: every refresh of the description renders it while
    :attr:`is_degraded` holds.  Only a restarted poller that completed a full
    pass including a liveness check clears it, and the same event ends the
    count of deaths in a row.
    """

    _deaths: int = 0
    _degraded: bool = False

    @property
    def is_degraded(self) -> bool:
        """Whether the user is to be told that notifications stopped."""
        return self._degraded

    def died(self) -> int:
        """Count one more death in a row and return the count."""
        self._deaths += 1
        return self._deaths

    def degrade(self) -> None:
        """Start telling the user that notifications stopped."""
        self._degraded = True

    def passed_liveness(self) -> bool:
        """Record a full pass with a liveness check; return whether it recovered."""
        was_degraded = self._degraded
        self._deaths = 0
        self._degraded = False
        return was_degraded
