"""Availability toggle — ``/mesg on|off``.

Controls whether the current user accepts messages, like BSD ``mesg(1)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import refresh_read_messages, set_biff_enabled
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the mesg toggle tool."""

    @mcp.tool(
        name="mesg",
        description=(
            "Control message reception. "
            "Use 'on' to accept messages, 'off' to block them."
        ),
    )
    @track_activity(state)
    async def mesg(enabled: bool) -> str:  # noqa: FBT001
        """Control whether this session accepts messages.

        Use enabled=True to accept messages or False to block them. This changes
        the session's availability, not repository-wide enablement.

        Returns:
            BSD-style confirmation: 'is y' for enabled or 'is n' for disabled.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        set_biff_enabled(enabled=enabled)
        state.emitter.set_mesg(enabled=enabled)
        await update_current_session(state, biff_enabled=enabled)
        await refresh_read_messages(mcp, state)
        return f"is {'y' if enabled else 'n'}"
