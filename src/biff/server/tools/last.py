"""Session history tool — ``/last``.

Shows login/logout history from the wtmp stream, matching Unix ``last(1)``.
Each row shows who logged in, from where, when, and for how long.
Sessions still active are marked "still logged in".
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_last, pair_events
from biff.server.tools._activate import auto_enable
from biff.server.tools._session import update_current_session
from biff.tty import build_session_key

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the last tool."""

    @mcp.tool(
        name="last",
        description="Show session login/logout history (like Unix last).",
    )
    @auto_enable(state)
    async def last(user: str = "", count: int = 25) -> str:
        """Show recent session history."""
        await update_current_session(state)
        count = max(1, min(count, 100))

        # Normalize user arg — strip @ prefix
        filter_user: str | None = None
        if user:
            filter_user = user.strip().lstrip("@")

        events = await state.relay.get_wtmp(user=filter_user, count=count * 2)
        if not events:
            return "No session history."

        current_sessions = await state.relay.get_sessions()
        active_keys = {build_session_key(s.user, s.tty) for s in current_sessions}

        pairs = pair_events(events)
        pairs = pairs[:count]

        return format_last(pairs, active_keys)
