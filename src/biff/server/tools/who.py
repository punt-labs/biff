"""Presence list tool — ``/who``.

Lists all sessions, showing idle time like ``w(1)``.
``+`` means accepting messages, ``-`` means messages off.
Each row represents one TTY session; a user with multiple
sessions appears on multiple rows.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_who
from biff.server.tools._activate import auto_enable
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
    @auto_enable(state)
    async def who(repo: str = "") -> str:
        """List all sessions with idle time.

        When *repo* is given (e.g. ``@punt-labs__vox``), show only
        sessions from that repo.  Otherwise shows all visible repos.
        """
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        sessions = await state.relay.get_sessions()
        visible = state.config.visible_repos
        if repo:
            if repo not in visible:
                return f"Repo {repo!r} is not in your visible repos."
            sessions = [s for s in sessions if s.repo == repo]
        else:
            sessions = [s for s in sessions if not s.repo or s.repo in visible]
        if not sessions:
            return "No sessions."
        sorted_sessions = sorted(sessions, key=lambda s: s.last_active, reverse=True)
        return format_who(sorted_sessions)
