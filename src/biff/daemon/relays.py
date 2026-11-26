"""One relay connection per distinct relay target, shared by every session."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

from biff.models import BiffConfig, RelayAuth
from biff.relay_select import RelaySelector

if TYPE_CHECKING:
    from collections.abc import Callable

    from biff.relay import Relay

__all__ = ["RelayFactory", "RelayPool", "RelayTarget", "ResolvedSession"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RelayTarget:
    """What identifies a relay: a NATS server with its credentials, or a directory.

    Two sessions share a connection exactly when their targets are equal.  A
    local relay is its own target per data directory, so the sessions of one
    repository share one and another repository's sessions do not.
    """

    url: str | None
    auth: RelayAuth | None
    tls_handshake_first: bool
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
                data_dir=None,
            )
        return RelayTarget(
            url=None, auth=None, tls_handshake_first=False, data_dir=self.data_dir
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
        """Close every relay; one that fails to close does not keep the rest open."""
        relays, self._relays = list(self._relays.values()), {}
        for relay in relays:
            try:
                await relay.close()
            except Exception:
                logger.warning("failed to close a relay", exc_info=True)
