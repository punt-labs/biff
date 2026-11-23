"""Choose the relay a process uses from its configuration."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff.nats_relay import NatsRelay
from biff.relay import DormantRelay, LocalRelay, Relay

if TYPE_CHECKING:
    from pathlib import Path

    from biff.models import BiffConfig

__all__ = ["RelaySelector"]


@final
class RelaySelector:
    """Picks the relay one configuration and data directory call for."""

    __slots__ = ("_config", "_data_dir")

    _config: BiffConfig
    _data_dir: Path

    def __new__(cls, config: BiffConfig, data_dir: Path) -> Self:
        """Create a selector for *config*, with *data_dir* for a local relay."""
        self = super().__new__(cls)
        self._config = config
        self._data_dir = data_dir
        return self

    def select(self, *, dormant: bool) -> Relay:
        """Return the relay the configuration selects.

        A *dormant* repo gets :class:`~biff.relay.DormantRelay`; otherwise
        ``relay_url`` selects :class:`~biff.nats_relay.NatsRelay`, and without
        one a :class:`~biff.relay.LocalRelay` under the data directory.
        """
        config = self._config
        if dormant:
            return DormantRelay()
        if config.relay_url:
            return NatsRelay(
                url=config.relay_url,
                auth=config.relay_auth,
                tls_handshake_first=config.relay_tls_handshake_first,
                name=f"biff-{config.repo_name}-{config.user}",
                repo_name=config.repo_name,
            )
        return LocalRelay(data_dir=self._data_dir)
