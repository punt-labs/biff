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
from biff.server.tools._session import resolve_tty_name, update_current_session
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
        """Query session and presence info for one or more users.

        Accepts space-separated addresses: ``@user1 @user2 @user3``.
        Each address can be ``@user`` (all sessions) or ``@user:tty``
        (specific session).
        """
        await update_current_session(state)
        await refresh_read_messages(mcp, state)

        addresses = user.split()
        all_sessions = await state.relay.get_sessions_for_repos(state.visible_repos)

        blocks: list[str] = []
        for addr in addresses:
            bare_user, tty = parse_address(addr)
            if tty:
                session = resolve_tty_name(
                    all_sessions,
                    bare_user,
                    tty,
                    local_repo=state.config.repo_name,
                )
                if session is None:
                    blocks.append(f"Login: {bare_user}\nNo session on tty {tty}.")
                else:
                    blocks.append(format_finger(session))
            else:
                sessions = [s for s in all_sessions if s.user == bare_user]
                if not sessions:
                    blocks.append(f"Login: {bare_user}\nNever logged in.")
                else:
                    blocks.append(format_finger_multi(sessions))
        return "\n\n".join(blocks)
