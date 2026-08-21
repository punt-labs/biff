"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.
``+`` means accepting messages, ``-`` means messages off.
Each row represents one TTY session; a user with multiple
sessions appears on multiple rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_dead_footnote, format_who
from biff.relay import dead_sessions, live_sessions
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the who tool."""

    @mcp.tool(
        name="who",
        description="List all active team members and what they're working on.",
    )
    @track_activity(state)
    async def who() -> str:
        """List all sessions with idle time."""
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        sessions = await state.relay.get_sessions_for_repos(state.visible_repos)
        live = live_sessions(sessions)
        footnote = format_dead_footnote(dead_sessions(sessions))
        if not live:
            text = "No sessions."
        else:
            # Sort by last_tool_at, matching the IDLE column format_who
            # renders (:func:`biff.formatting.format_who`) -- last_active is
            # heartbeat recency, unrelated to the idle value shown.
            sorted_sessions = sorted(live, key=lambda s: s.last_tool_at, reverse=True)
            text = format_who(sorted_sessions)
        return f"{text}\n{footnote}" if footnote else text
