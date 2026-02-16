"""User status query tool — ``/finger @user``.

Shows what a user is working on, when they were last active,
and whether they're accepting messages.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_check_messages
from biff.server.tools._session import update_current_session

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

        Output mimics BSD ``finger(1)`` two-column layout::

            Login: kai                        Messages: on
            On since Sun Feb 15 14:01 (UTC) on claude, idle 0:03
            Plan:
             refactoring auth module
        """
        await update_current_session(state)
        await refresh_check_messages(mcp, state)
        bare = user.strip().lstrip("@")
        session = await state.relay.get_session(bare)
        if session is None:
            return f"Login: {bare}\nNever logged in."
        idle = _format_idle(session.last_active)
        since = session.last_active.strftime("%a %b %d %H:%M (%Z)")
        mesg = "on" if session.biff_enabled else "off"

        left = f"Login: {bare}"
        if session.display_name:
            # Name on first line, Messages on second
            right = f"Name: {session.display_name}"
            line1 = f"{left:<38s}{right}"
            line2 = f"Messages: {mesg}"
        else:
            # Original layout: Messages on first line
            right = f"Messages: {mesg}"
            line1 = f"{left:<38s}{right}"
            line2 = ""

        line_on = f"On since {since} on claude, idle {idle}"
        plan_block = f"Plan:\n {session.plan}" if session.plan else "No Plan."

        lines = [line1]
        if line2:
            lines.append(line2)
        lines.append(line_on)
        lines.append(plan_block)
        return "\n".join(lines)
