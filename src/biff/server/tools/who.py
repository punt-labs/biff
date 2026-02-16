"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.
``+`` means accepting messages, ``-`` means messages off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._formatting import format_idle
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def _sanitize_plan(plan: str) -> str:
    """Sanitize plan text so it doesn't break pipe-separated output."""
    return plan.replace("|", "/").replace("\n", " ").replace("\r", " ")


def _format_session(s: UserSession) -> str:
    """Format one session as ``@user IDLE +/- plan``."""
    idle = format_idle(s.last_active)
    flag = "+" if s.biff_enabled else "-"
    plan = _sanitize_plan(s.plan) if s.plan else "(no plan)"
    return f"@{s.user} {idle} {flag} {plan}"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the who tool."""

    @mcp.tool(
        name="who",
        description="List all active team members and what they're working on.",
    )
    async def who() -> str:
        """List all sessions with idle time."""
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        sessions = await state.relay.get_sessions()
        if not sessions:
            return ""
        sorted_sessions = sorted(sessions, key=lambda s: s.user)
        return " | ".join(_format_session(s) for s in sorted_sessions)
