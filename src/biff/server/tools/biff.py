"""Availability toggle — ``/biff on|off``.

Controls whether the current user accepts messages and appears active.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_check_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff toggle tool."""

    @mcp.tool(
        name="biff",
        description=(
            "Control message reception. "
            "Use 'on' to accept messages, 'off' to block them."
        ),
    )
    def biff(enabled: bool) -> str:  # noqa: FBT001
        """Toggle message reception for the current user."""
        update_current_session(state, biff_enabled=enabled)
        refresh_check_messages(mcp, state)
        status = "on" if enabled else "off"
        return f"Messages are now {status} for @{state.config.user}."
