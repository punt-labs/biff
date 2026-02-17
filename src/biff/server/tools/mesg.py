"""Availability toggle — ``/mesg on|off``.

Controls whether the current user accepts messages, like BSD ``mesg(1)``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    async def mesg(enabled: bool) -> str:  # noqa: FBT001
        """Toggle message reception for the current user.

        Output mimics BSD ``mesg(1)``::

            is y
        """
        set_biff_enabled(enabled=enabled)
        await update_current_session(state, biff_enabled=enabled)
        await refresh_read_messages(mcp, state)
        return f"is {'y' if enabled else 'n'}"
