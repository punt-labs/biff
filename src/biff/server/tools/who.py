"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.
``+`` means accepting messages, ``-`` means messages off.
Each row represents one TTY session; a user with multiple
sessions appears on multiple rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import UserSession
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._formatting import (
    ColumnSpec,
    format_idle,
    format_table,
    last_component,
)
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_WHO_SPECS: list[ColumnSpec] = [
    ColumnSpec("NAME", min_width=4),
    ColumnSpec("TTY", min_width=3),
    ColumnSpec("IDLE", min_width=4),
    ColumnSpec("S", min_width=1),
    ColumnSpec("HOST", min_width=4),
    ColumnSpec("DIR", min_width=3),
    ColumnSpec("PLAN", min_width=10, fixed=False),
]


def _sanitize_plan(plan: str) -> str:
    """Collapse newlines so plan text stays on one row."""
    return plan.replace("\n", " ").replace("\r", " ")


def _format_who(sessions: list[UserSession]) -> str:
    """Build a columnar table matching ``w(1)`` style with host and dir."""
    rows: list[list[str]] = [
        [
            f"@{s.user}",
            s.tty_name or (s.tty[:8] if s.tty else "-"),
            format_idle(s.last_active),
            "+" if s.biff_enabled else "-",
            s.hostname or "-",
            last_component(s.pwd) if s.pwd else "-",
            _sanitize_plan(s.plan) if s.plan else "(no plan)",
        ]
        for s in sessions
    ]
    return format_table(_WHO_SPECS, rows)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the who tool."""

    @mcp.tool(
        name="who",
        description="List all active team members and what they're working on.",
    )
    @auto_enable(state)
    async def who() -> str:
        """List all sessions with idle time."""
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        sessions = await state.relay.get_sessions()
        if not sessions:
            return "No sessions."
        sorted_sessions = sorted(sessions, key=lambda s: s.last_active, reverse=True)
        return _format_who(sorted_sessions)
