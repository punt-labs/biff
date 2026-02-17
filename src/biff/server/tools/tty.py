"""Session naming tool — ``/tty [name]``.

Names the current session for easier identification in ``/who`` output.
Without arguments, auto-assigns the next sequential ``ttyN``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_MAX_TTY_NAME = 20
_TTY_SEQ_RE = re.compile(r"^tty(\d+)$")


def _next_tty_name(existing_names: list[str]) -> str:
    """Return the next sequential ``ttyN`` not already in use."""
    highest = 0
    for name in existing_names:
        m = _TTY_SEQ_RE.match(name)
        if m:
            highest = max(highest, int(m.group(1)))
    return f"tty{highest + 1}"


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the tty tool."""

    @mcp.tool(
        name="tty",
        description=(
            "Name the current session. Visible in /who and /finger TTY column."
        ),
    )
    async def tty(name: str = "") -> str:
        """Set a human-readable name for this session.

        If *name* is omitted, auto-assigns the next ``ttyN``.
        Names are limited to 20 characters.

        Output echoes the name::

            TTY: tty3
        """
        if not name:
            sessions = await state.relay.get_sessions()
            existing = [s.tty_name for s in sessions if s.tty_name]
            name = _next_tty_name(existing)

        if len(name) > _MAX_TTY_NAME:
            return f"Error: name must be {_MAX_TTY_NAME} characters or fewer."

        await update_current_session(state, tty_name=name)
        await refresh_read_messages(mcp, state)
        return f"TTY: {name}"
