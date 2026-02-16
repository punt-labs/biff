"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.
``+`` means accepting messages, ``-`` means messages off.
Each row represents one TTY session; a user with multiple
sessions appears on multiple rows.
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
    """Build a columnar table matching ``w(1)`` style with host and dir."""
    rows: list[tuple[str, str, str, str, str, str, str]] = []
    for s in sessions:
        name = f"@{s.user}"
        tty = s.tty[:8] if s.tty else "-"
        idle = format_idle(s.last_active)
        flag = "+" if s.biff_enabled else "-"
        host = s.hostname or "-"
        pwd = s.pwd or "-"
        plan = _sanitize_plan(s.plan) if s.plan else "(no plan)"
        rows.append((name, tty, idle, flag, host, pwd, plan))

    name_w = max(4, max(len(r[0]) for r in rows))
    tty_w = max(3, max(len(r[1]) for r in rows))
    idle_w = max(4, max(len(r[2]) for r in rows))
    host_w = max(4, max(len(r[4]) for r in rows))
    pwd_w = max(3, max(len(r[5]) for r in rows))

    header = (
        f"\u25b6  {'NAME':<{name_w}}  {'TTY':<{tty_w}}  {'IDLE':<{idle_w}}"
        f"  S  {'HOST':<{host_w}}  {'DIR':<{pwd_w}}  PLAN"
    )
    lines: list[str] = []
    for name, tty, idle, flag, host, pwd, plan in rows:
        lines.append(
            f"   {name:<{name_w}}  {tty:<{tty_w}}  {idle:<{idle_w}}"
            f"  {flag}  {host:<{host_w}}  {pwd:<{pwd_w}}  {plan}"
        )
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
        sorted_sessions = sorted(sessions, key=lambda s: (s.user, s.tty))
        return _format_table(sorted_sessions)
