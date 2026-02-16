"""Presence list tool — ``/who``.

Lists all active sessions within the TTL window.
Output mirrors Unix ``who`` conventions: ``+`` means accepting
messages, ``-`` means messages off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._descriptions import refresh_check_messages

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_DEFAULT_TTL = 120


def _sanitize_plan(plan: str) -> str:
    """Sanitize plan text so it doesn't break pipe-separated output."""
    return plan.replace("|", "/").replace("\n", " ").replace("\r", " ")


def _format_session(s: UserSession) -> str:
    """Format one session as ``@user +/- HH:MM plan``."""
    flag = "+" if s.biff_enabled else "-"
    time_str = s.last_active.strftime("%H:%M")
    plan = _sanitize_plan(s.plan) if s.plan else "(no plan)"
    return f"@{s.user} {flag} {time_str} {plan}"


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
            return ""
        sorted_sessions = sorted(active, key=lambda s: s.user)
        return " | ".join(_format_session(s) for s in sorted_sessions)
