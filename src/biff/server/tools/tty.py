"""Session naming tool — ``/tty <name>``.

Names the current session for easier identification in ``/who`` output.
Unnamed sessions keep the auto-generated hex TTY identifier.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the tty tool."""

    @mcp.tool(
        name="tty",
        description=(
            "Name the current session. Visible in /who and /finger TTY column."
        ),
    )
    async def tty(name: str) -> str:
        """Set a human-readable name for this session.

        Output echoes the name::

            TTY: my-feature-work
        """
        await update_current_session(state, tty_name=name)
        await refresh_read_messages(mcp, state)
        return f"TTY: {name}"
