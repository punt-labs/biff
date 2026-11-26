"""The repos of a session's orgs, rediscovered while the session lives."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Protocol, Self, final, runtime_checkable

from biff.relay_errors import RELAY_ERRORS, RelayLog

if TYPE_CHECKING:
    from collections.abc import Sequence

    from biff.relay import Relay

__all__ = ["OrgRepos", "RepoDiscovery"]

logger = logging.getLogger(__name__)


@runtime_checkable
class RepoDiscovery(Protocol):
    """A relay that can list the repos of an org (the NATS relay can)."""

    async def discover_repos_for_orgs(self, orgs: Sequence[str]) -> frozenset[str]:
        """Return every repo of *orgs* the relay knows."""
        ...


@final
class OrgRepos:
    """The repos a session sees through its orgs.

    A teammate can start a session in a repo the org gained after this one began,
    and mail to it would go unseen if discovery ran once.  :meth:`refresh`
    rediscovers at most once per interval, as the in-process server does.  The
    attempt time is recorded before asking, so a failing relay is not retried
    hot, and an empty or failed answer keeps what is already known.
    """

    __slots__ = ("_found", "_interval", "_last", "_log", "_orgs", "_relay")

    _relay: Relay
    _orgs: tuple[str, ...]
    _interval: float
    _found: frozenset[str]
    _last: float | None  # monotonic time of the last attempt; none before the first
    _log: RelayLog

    def __new__(cls, relay: Relay, orgs: tuple[str, ...], interval: float) -> Self:
        """Create discovery of *orgs* through *relay*, at most once per *interval*."""
        self = super().__new__(cls)
        self._relay = relay
        self._orgs = orgs
        self._interval = interval
        self._found = frozenset()
        self._last = None
        self._log = RelayLog(logger)
        return self

    @property
    def current(self) -> frozenset[str]:
        """The repos found so far."""
        return self._found

    async def refresh(self) -> bool:
        """Rediscover if the interval has passed; return whether the repos changed."""
        relay = self._relay
        if not self._orgs or not isinstance(relay, RepoDiscovery):
            return False
        now = time.monotonic()
        if self._last is not None and now - self._last < self._interval:
            return False
        self._last = now
        try:
            found = await relay.discover_repos_for_orgs(self._orgs)
        except RELAY_ERRORS as exc:
            self._log.failed("org discovery", exc)
            return False
        self._log.recovered("org discovery")
        if not found or found == self._found:
            return False
        self._found = found
        return True
