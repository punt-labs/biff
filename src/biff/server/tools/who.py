"""Presence list tool — ``/who``.

Lists all active sessions within the TTL window.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_check_messages

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
    async def who() -> str:
        """List active sessions."""
        await refresh_check_messages(mcp, state)
        active = await state.relay.get_active_sessions(ttl=_DEFAULT_TTL)
        if not active:
            return "No active sessions."
        lines = [f"@{s.user} — {s.plan or '(no plan)'}" for s in active]
        return "\n".join(lines)
