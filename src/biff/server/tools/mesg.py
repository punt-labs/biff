"""Message availability tool — ``/mesg on|off``.

Controls whether the current user accepts messages.
Stub implementation for the server scaffold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the mesg tool."""

    @mcp.tool(
        name="mesg",
        description=(
            "Control message reception. "
            "Use 'on' to accept messages, 'off' to block them."
        ),
    )
    def mesg(enabled: bool) -> str:  # noqa: FBT001
        """Toggle message reception for the current user."""
        session = state.sessions.get_user(state.config.user)
        if session is None:
            state.sessions.heartbeat(state.config.user)
            session = state.sessions.get_user(state.config.user)
            assert session is not None  # noqa: S101
        state.sessions.update(session.model_copy(update={"biff_enabled": enabled}))
        status = "on" if enabled else "off"
        return f"Messages are now {status} for @{state.config.user}."
