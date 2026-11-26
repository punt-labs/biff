"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.  ``+`` means accepting
messages, ``-`` means messages off.  Each row represents one TTY session; a
user with multiple sessions appears on multiple rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_who, log_dead_sessions
from biff.nostr_directory import NostrDirectory
from biff.presence_liveness import live_sessions
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
        """Find teammates and session addresses in visible repositories.

        Call without arguments to inspect live sessions, idle times, and plans.

        Returns:
            A presence table, or 'No sessions.' when none are live. Sessions
            that stopped responding are logged, not shown.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        sessions = await state.relay.get_sessions_for_repos(state.visible_repos)
        live = live_sessions(sessions)
        log_dead_sessions(sessions)
        if not live:
            return "No sessions."
        # Sort by last_tool_at to match the IDLE column, not heartbeat recency.
        keys = await NostrDirectory.for_repo(state.repo_root).session_npubs(live)
        return format_who(
            sorted(live, key=lambda s: s.last_tool_at, reverse=True), keys
        )
