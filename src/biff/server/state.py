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
from biff.tty import build_session_key, generate_tty, get_hostname, get_pwd


@dataclass(frozen=True)
class ServerState:
    """Immutable container for all server-wide shared state."""

    config: BiffConfig
    relay: Relay
    tty: str = ""
    hostname: str = ""
    pwd: str = ""
    unread_path: Path | None = None
    owns_relay: bool = True

    @property
    def session_key(self) -> str:
        """Composite key ``{user}:{tty}`` for this server instance."""
        return build_session_key(self.config.user, self.tty)


def create_state(
    config: BiffConfig,
    data_dir: Path,
    *,
    relay: Relay | None = None,
    unread_path: Path | None = None,
    tty: str | None = None,
    hostname: str | None = None,
    pwd: str | None = None,
) -> ServerState:
    """Create a ``ServerState`` from config and data directory.

    Relay selection: an explicit *relay* wins, then ``config.relay_url``
    selects :class:`~biff.nats_relay.NatsRelay`, otherwise
    :class:`~biff.relay.LocalRelay`.

    Runtime identity (tty, hostname, pwd) is auto-generated when not
    provided — each server instance gets a unique session key.
    """
    owns_relay = relay is None
    if relay is None:
        if config.relay_url:
            relay = NatsRelay(
                url=config.relay_url,
                auth=config.relay_auth,
                name=f"biff-{config.repo_name}-{config.user}",
                repo_name=config.repo_name,
            )
        else:
            relay = LocalRelay(data_dir=data_dir)
    return ServerState(
        config=config,
        relay=relay,
        tty=tty or generate_tty(),
        hostname=hostname or get_hostname(),
        pwd=pwd or get_pwd(),
        unread_path=unread_path,
        owns_relay=owns_relay,
    )
