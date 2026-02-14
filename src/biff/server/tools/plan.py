"""Status set tool — ``/plan "msg"``.

Sets the current user's plan (what they're working on).
Stub implementation for the server scaffold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the plan tool."""

    @mcp.tool(
        name="plan",
        description=(
            "Set what you're currently working on. "
            "Visible to teammates via /finger and /who."
        ),
    )
    def plan(message: str) -> str:
        """Update the current user's plan."""
        session = state.sessions.get_user(state.config.user)
        if session is None:
            state.sessions.heartbeat(state.config.user)
            session = state.sessions.get_user(state.config.user)
            assert session is not None  # noqa: S101
        state.sessions.update(session.model_copy(update={"plan": message}))
        return f"Plan updated: {message}"
