"""The request and timeout counters a relay may keep, for ``biff status``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from biff.relay import Relay

__all__ = ["CountingRelay", "RelayCounters"]


@runtime_checkable
class CountingRelay(Protocol):
    """A relay that counts the requests it makes and how many timed out.

    The NATS relay does; the local relay makes no requests and does not.
    """

    @property
    def total_attempts(self) -> int:
        """Requests made since the relay was created."""
        raise NotImplementedError

    @property
    def total_timeouts(self) -> int:
        """Requests that timed out since the relay was created."""
        raise NotImplementedError


class RelayCounters:
    """How ``biff status`` reports a relay's counters."""

    __slots__ = ()

    @staticmethod
    def json_of(relay: Relay) -> dict[str, int] | None:
        """Return ``{"attempts", "timeouts"}`` for a counting *relay*, else ``None``."""
        if isinstance(relay, CountingRelay):
            return {"attempts": relay.total_attempts, "timeouts": relay.total_timeouts}
        return None
