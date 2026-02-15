"""Server state container for biff MCP tools.

All shared state lives in a frozen dataclass. Tool closures capture it
directly during registration; it is also available as the lifespan context.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from biff.models import BiffConfig
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
    """Create a ``ServerState`` from config and data directory."""
    return ServerState(
        config=config,
        relay=relay or LocalRelay(data_dir=data_dir),
        unread_path=unread_path,
    )
