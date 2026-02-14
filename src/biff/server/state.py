"""Server state container for biff MCP tools.

All shared state lives in a frozen dataclass passed through FastMCP's
lifespan mechanism. Tools receive it via ``ctx.request_context.lifespan_context``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from biff.models import BiffConfig
from biff.storage import MessageStore, SessionStore


@dataclass(frozen=True)
class ServerState:
    """Immutable container for all server-wide shared state."""

    config: BiffConfig
    messages: MessageStore
    sessions: SessionStore


def create_state(config: BiffConfig, data_dir: Path) -> ServerState:
    """Create a ``ServerState`` from config and data directory."""
    return ServerState(
        config=config,
        messages=MessageStore(data_dir=data_dir),
        sessions=SessionStore(data_dir=data_dir),
    )
