"""Status set tool — ``/plan "msg"``.

Sets the current user's plan (what they're working on).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_check_messages
from biff.server.tools._session import update_current_session

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
    async def plan(message: str) -> str:
        """Update the current user's plan."""
        await update_current_session(state, plan=message)
        await refresh_check_messages(mcp, state)
        return f"Plan updated: {message}"
