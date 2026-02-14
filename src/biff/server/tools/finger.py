"""User status query tool — ``/finger @user``.

Shows what a user is working on, when they were last active,
and whether they're accepting messages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_check_messages

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def _format_last_active(dt: datetime) -> str:
    """Format as relative time with full date.

    Example: ``3m ago (Fri Feb 14 16:34 UTC)``
    """
    now = datetime.now(UTC)
    delta = now - dt
    total_seconds = max(0, int(delta.total_seconds()))

    if total_seconds < 60:
        relative = "just now"
    elif total_seconds < 3600:
        relative = f"{total_seconds // 60}m ago"
    elif total_seconds < 86400:
        relative = f"{total_seconds // 3600}h ago"
    else:
        relative = f"{total_seconds // 86400}d ago"

    absolute = dt.strftime("%a %b %d %H:%M UTC")
    return f"{relative} ({absolute})"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the finger tool."""

    @mcp.tool(
        name="finger",
        description="Check what a user is working on and their availability.",
    )
    def finger(user: str) -> str:
        """Query a user's session and presence info."""
        refresh_check_messages(mcp, state)
        bare = user.strip().lstrip("@")
        session = state.sessions.get_user(bare)
        if session is None:
            return f"@{bare} has no active session."
        status = "accepting messages" if session.biff_enabled else "messages off"
        plan_line = f"  Plan: {session.plan}" if session.plan else "  No plan set."
        return (
            f"@{bare} — {status}\n"
            f"  Last active: {_format_last_active(session.last_active)}\n"
            f"{plan_line}"
        )
