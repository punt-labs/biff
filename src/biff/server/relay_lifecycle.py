"""Relay registration and failure boundaries for the MCP lifecycle."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Generator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR

from biff.models import RelayConnectError
from biff.session_registry import register_session, write_marker

if TYPE_CHECKING:
    from biff.server.state import ServerState

logger = logging.getLogger(__name__)


class RelayLifecycle:
    """Keep relay failures separate from MCP transport availability."""

    @staticmethod
    @contextmanager
    def startup() -> Generator[None]:
        """Allow MCP discovery even when initial session registration fails."""
        try:
            yield
        except (RelayConnectError, TimeoutError):
            logger.warning("Relay connection failed during startup")

    @staticmethod
    @contextmanager
    def tool_call() -> Generator[None]:
        """Translate relay attachment failures into safe protocol errors."""
        try:
            yield
        except (RelayConnectError, TimeoutError):
            raise MCPError(INTERNAL_ERROR, "Relay connection failed") from None

    @staticmethod
    async def close(state: ServerState, timeout: float) -> None:
        """Close the relay within the teardown budget.

        Args:
            state: Server whose relay is closing.
            timeout: Maximum seconds to wait for shutdown.
        """
        try:
            await asyncio.wait_for(state.relay.close(), timeout=timeout)
        except Exception:
            logger.warning("Failed to close relay", exc_info=True)

    @staticmethod
    async def register(state: ServerState, on_claim: Callable[[str], None]) -> str:
        """Register presence before publishing the local session marker.

        Args:
            state: Session to register.
            on_claim: Record an alias reservation for startup cleanup.
        """
        _, name = await register_session(
            state.relay,
            state.config.user,
            state.tty,
            display_name=state.config.display_name,
            kind=state.config.kind,
            hostname=state.hostname,
            pwd=state.pwd,
            repo=state.config.repo_name,
            launched_by=state.config.launched_by,
            owner_git=state.config.owner_git,
            on_claim=on_claim,
        )
        write_marker(
            state.config.repo_name, state.session_key, str(state.repo_common_root)
        )
        return name
