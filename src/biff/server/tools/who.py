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
    """Collapse newlines so plan text stays on one row."""
    return plan.replace("\n", " ").replace("\r", " ")


def _format_table(sessions: list[UserSession]) -> str:
    """Build a columnar table matching ``w(1)`` style."""
    rows: list[tuple[str, str, str, str]] = []
    for s in sessions:
        name = f"@{s.user}"
        idle = format_idle(s.last_active)
        flag = "+" if s.biff_enabled else "-"
        plan = _sanitize_plan(s.plan) if s.plan else "(no plan)"
        rows.append((name, idle, flag, plan))

    name_w = max(4, max(len(r[0]) for r in rows))
    idle_w = max(4, max(len(r[1]) for r in rows))

    header = f"\u25b6  {'NAME':<{name_w}}  {'IDLE':<{idle_w}}  S  PLAN"
    lines: list[str] = []
    for name, idle, flag, plan in rows:
        lines.append(f"   {name:<{name_w}}  {idle:<{idle_w}}  {flag}  {plan}")
    return header + "\n" + "\n".join(lines)


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
            return "No sessions."
        sorted_sessions = sorted(sessions, key=lambda s: s.user)
        return _format_table(sorted_sessions)
