"""Server state container for biff MCP tools.

All shared state lives in a frozen dataclass. Tool closures capture it
directly during registration; it is also available as the lifespan context.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay, Relay


@dataclass(frozen=True)
class ServerState:
    """Immutable container for all server-wide shared state."""

    config: BiffConfig
    relay: Relay
    unread_path: Path | None = None


def create_state(
    config: BiffConfig,
    data_dir: Path,
    *,
    relay: Relay | None = None,
    unread_path: Path | None = None,
) -> ServerState:
    """Create a ``ServerState`` from config and data directory.

    Relay selection: an explicit *relay* wins, then ``config.relay_url``
    selects :class:`~biff.nats_relay.NatsRelay`, otherwise
    :class:`~biff.relay.LocalRelay`.
    """
    if relay is None:
        if config.relay_url:
            relay = NatsRelay(url=config.relay_url)
        else:
            relay = LocalRelay(data_dir=data_dir)
    return ServerState(
        config=config,
        relay=relay,
        unread_path=unread_path,
    )
