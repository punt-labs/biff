"""Session naming tool — ``/tty [name]``.

Names the current session for easier identification in ``/who`` output.
Without arguments, auto-assigns the next sequential ``ttyN``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages, set_tty_name
from biff.server.tools._session import update_current_session
from biff.tty import assign_unique_tty_name

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_MAX_TTY_NAME = 20


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the tty tool."""

    @mcp.tool(
        name="tty",
        description=(
            "Name the current session. Visible in /who and /finger TTY column."
        ),
    )
    @auto_enable(state)
    async def tty(name: str = "") -> str:
        """Set a human-readable name for this session.

        If *name* is omitted, auto-assigns the next ``ttyN``.
        Names are limited to 20 characters.

        Output echoes the name::

            TTY: tty3
        """
        name = name.strip()

        if not name:
            name = await assign_unique_tty_name(state.relay, state.session_key)

        sessions = await state.relay.get_sessions()

        if len(name) > _MAX_TTY_NAME:
            return f"Error: name must be {_MAX_TTY_NAME} characters or fewer."

        # Reject duplicate names for the same user
        for s in sessions:
            if (
                s.user == state.config.user
                and s.tty != state.tty
                and s.tty_name == name
            ):
                return f"Error: name {name!r} already in use by another session."

        set_tty_name(name)
        await update_current_session(state, tty_name=name)
        await refresh_read_messages(mcp, state)
        return f"TTY: {name}"
