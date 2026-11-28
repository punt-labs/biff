"""One relay connection per distinct relay target, shared by every session."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.models import BiffConfig, RelayAuth
from biff.relay_errors import RELAY_ERRORS
from biff.relay_select import RelaySelector

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.relay import Relay

__all__ = ["RelayFactory", "RelayPool", "RelayTarget", "ResolvedSession"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayTarget:
    """What identifies a relay: a NATS server, its credentials and a repository.

    Two sessions share a connection exactly when their targets are equal.
    ``NatsRelay`` scopes its subjects, presence and wall by repository name, so
    two repositories on one NATS server are two targets; sharing one NATS
    client between them is a later optimization.  A local relay is its own
    target per data directory.
    """

    url: str | None
    auth: RelayAuth | None
    tls_handshake_first: bool
    repo_name: str | None
    data_dir: Path | None


@dataclass(frozen=True)
class ResolvedSession:
    """The configuration a session's hello resolved to."""

    config: BiffConfig
    data_dir: Path
    repo_root: Path | None

    @property
    def target(self) -> RelayTarget:
        """The relay this session talks to."""
        config = self.config
        if config.relay_url:
            return RelayTarget(
                url=config.relay_url,
                auth=config.relay_auth,
                tls_handshake_first=config.relay_tls_handshake_first,
                repo_name=config.repo_name,
                data_dir=None,
            )
        return RelayTarget(
            url=None,
            auth=None,
            tls_handshake_first=False,
            repo_name=None,
            data_dir=self.data_dir,
        )


type RelayFactory = Callable[[ResolvedSession], Relay]


@final
class RelayPool:
    """Opens a relay the first time a target is used and hands it to later users.

    The factory runs without awaiting, so two sessions arriving together for one
    target cannot both open a connection.
    """

    __slots__ = ("_factory", "_relays")

    _factory: RelayFactory
    _relays: dict[RelayTarget, Relay]

    def __new__(cls, factory: RelayFactory) -> Self:
        """Create an empty pool that builds relays with *factory*."""
        self = super().__new__(cls)
        self._factory = factory
        self._relays = {}
        return self

    @staticmethod
    def selecting() -> RelayFactory:
        """Return the factory that builds the relay a session's config selects."""
        return lambda resolved: RelaySelector(
            resolved.config, resolved.data_dir
        ).select(dormant=False)

    @property
    def connection_count(self) -> int:
        """The number of relay connections the pool holds."""
        return len(self._relays)

    def acquire(self, resolved: ResolvedSession) -> Relay:
        """Return the shared relay for *resolved*'s target, opening it once."""
        target = resolved.target
        relay = self._relays.get(target)
        if relay is None:
            relay = self._factory(resolved)
            self._relays[target] = relay
        return relay

    async def close(self) -> None:
        """Close every relay, all at once, so one slow peer does not delay the rest.

        A relay that fails to close for a reason a dead peer explains is logged
        and does not keep the rest open.  Any other failure is a bug: it is
        raised, as an exception group, only after every relay has been closed.

        Raises:
            ExceptionGroup: One or more relays failed to close for a reason that
                is not a relay outage.
        """
        relays, self._relays = list(self._relays.values()), {}
        outcomes = await asyncio.gather(*(self._close_one(r) for r in relays))
        if bugs := [exc for exc in outcomes if exc is not None]:
            msg = "relays failed to close"
            raise ExceptionGroup(msg, bugs)

    @staticmethod
    async def _close_one(relay: Relay) -> Exception | None:
        """Close *relay*; return the failure that is a bug, if there was one."""
        try:
            await relay.close()
        except RELAY_ERRORS:
            logger.warning("failed to close a relay", exc_info=True)
        except Exception as exc:
            logger.exception("a relay failed to close")
            return exc
        return None
