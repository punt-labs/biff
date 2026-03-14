"""User status query tool — ``/finger @user``.

Shows what a user is working on, when they were last active,
and whether they're accepting messages.  Supports ``@user``
(shows all sessions) and ``@user:tty`` (shows one session).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_finger, format_finger_multi
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import resolve_session, update_current_session
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the finger tool."""

    @mcp.tool(
        name="finger",
        description="Check what a user is working on and their availability.",
    )
    @auto_enable(state)
    async def finger(user: str) -> str:
        """Query a user's session and presence info.

        ``@user`` shows all sessions for that user.
        ``@user:tty`` shows a specific session.
        """
        await update_current_session(state)
        await refresh_read_messages(mcp, state)
        bare_user, tty = parse_address(user)

        if tty:
            # Targeted: resolve by hex ID or tty_name
            session = await resolve_session(state.relay, bare_user, tty)
            if session is None:
                return f"Login: {bare_user}\nNo session on tty {tty}."
            visible = state.config.visible_repos
            if session.repo and session.repo not in visible:
                return f"Login: {bare_user}\nNo session on tty {tty}."
            return format_finger(session)

        # Bare user: show all sessions from visible repos
        sessions = await state.relay.get_sessions_for_user(bare_user)
        visible = state.config.visible_repos
        sessions = [s for s in sessions if s.repo in visible]
        if not sessions:
            return f"Login: {bare_user}\nNever logged in."
        return format_finger_multi(sessions)
