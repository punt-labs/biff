"""Presence list tool — ``/who``.

Lists all active sessions within the TTL window.
Stub implementation for the server scaffold.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_DEFAULT_TTL = 120


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the who tool."""

    @mcp.tool(
        name="who",
        description="List all active team members and what they're working on.",
    )
    def who() -> str:
        """List active sessions."""
        active = state.sessions.get_active(ttl=_DEFAULT_TTL)
        if not active:
            return "No active sessions."
        lines = [f"@{s.user} — {s.plan or '(no plan)'}" for s in active]
        return "\n".join(lines)
