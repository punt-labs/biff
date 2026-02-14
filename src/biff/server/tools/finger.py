"""User status query tool — ``/finger @user``.

Shows what a user is working on, when they were last active,
and whether they're accepting messages.
Stub implementation for the server scaffold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the finger tool."""

    @mcp.tool(
        name="finger",
        description="Check what a user is working on and their availability.",
    )
    def finger(user: str) -> str:
        """Query a user's session and presence info."""
        session = state.sessions.get_user(user)
        if session is None:
            return f"@{user} has no active session."
        status = "accepting messages" if session.biff_enabled else "messages off"
        plan_line = f"  Plan: {session.plan}" if session.plan else "  No plan set."
        return (
            f"@{user} — {status}\n"
            f"  Last active: {session.last_active.isoformat()}\n"
            f"{plan_line}"
        )
