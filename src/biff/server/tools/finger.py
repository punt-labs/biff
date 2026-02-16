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


def _format_idle(dt: datetime) -> str:
    """Format idle time matching BSD ``finger(1)`` style.

    Examples: ``0:03``, ``3:45``, ``1 day 7:22``
    """
    now = datetime.now(UTC)
    total_seconds = max(0, int((now - dt).total_seconds()))
    minutes = total_seconds // 60
    hours = minutes // 60
    days = hours // 24

    if days > 0:
        return f"{days} day{'s' if days > 1 else ''} {hours % 24}:{minutes % 60:02d}"
    return f"{hours}:{minutes % 60:02d}"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the finger tool."""

    @mcp.tool(
        name="finger",
        description="Check what a user is working on and their availability.",
    )
    async def finger(user: str) -> str:
        """Query a user's session and presence info.

        Output mimics BSD ``finger(1)``::

            Login: kai
            On since Sun Feb 15 14:01 (UTC) on claude, idle 3m (messages on)
            Plan: refactoring auth
        """
        await refresh_check_messages(mcp, state)
        bare = user.strip().lstrip("@")
        session = await state.relay.get_session(bare)
        if session is None:
            return f"Login: {bare}\nNever logged in."
        idle = _format_idle(session.last_active)
        since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
        mesg = "messages on" if session.biff_enabled else "messages off"
        plan = f"Plan: {session.plan}" if session.plan else "No Plan."
        return (
            f"Login: {bare}\nOn since {since} on claude, idle {idle} ({mesg})\n{plan}"
        )
