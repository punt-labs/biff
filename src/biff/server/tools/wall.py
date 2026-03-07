"""Team broadcast tool — ``wall``.

``/wall`` posts a time-limited banner visible to all teammates on their
status bar and tool descriptions.  Unlike ``/write``, wall messages do
not go into inboxes and do not require ``/read``.

BSD ``wall(1)`` wrote to every logged-in terminal.  Biff adds
duration-based persistence and explicit clearing.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import ValidationError

from biff.formatting import (
    format_remaining,
    format_wall,
    parse_duration,
    sanitize_wall_message,
)
from biff.models import WallPost
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_wall
from biff.server.tools._session import update_current_session
from biff.server.tools._tasks import fire_and_forget

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_log = logging.getLogger(__name__)


WALL_BASE_DESCRIPTION = (
    "Broadcast a message to the whole team. "
    "The message appears on every teammate's status bar and tool list. "
    "Use clear=True to remove an active wall."
)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the wall tool."""

    @mcp.tool(name="wall", description=WALL_BASE_DESCRIPTION)
    @auto_enable(state)
    async def wall(message: str = "", duration: str = "", clear: bool = False) -> str:
        """Post, read, or clear a team broadcast wall.

        Three modes:

        - ``wall(message="text")`` — post a wall (default 1h TTL)
        - ``wall(message="text", duration="2h")`` — post with explicit TTL
        - ``wall(clear=True)`` — remove the active wall
        - ``wall()`` — show the current wall
        """
        session = await update_current_session(state)

        # Clear mode
        if clear:
            await refresh_wall(mcp, state, wall=None)
            fire_and_forget(
                state.relay.set_wall(None), logger=_log, description="wall clear"
            )
            return "Wall cleared."

        # Sanitize: strip control chars, collapse to single line
        message = sanitize_wall_message(message)
        if not message:
            current = await state.relay.get_wall()
            if current is None:
                await refresh_wall(mcp, state, wall=None)
                return "No active wall."
            return format_wall(current)

        # Post mode
        try:
            ttl = parse_duration(duration)
        except ValueError as exc:
            return str(exc)

        now = datetime.now(UTC)
        message = message[:512]
        try:
            post = WallPost(
                text=message,
                from_user=state.config.user,
                from_tty=session.tty_name or session.tty,
                posted_at=now,
                expires_at=now + ttl,
            )
        except ValidationError as exc:
            for err in exc.errors():
                if err.get("type") == "string_too_short":
                    return "Message cannot be blank."
            return str(exc)
        await refresh_wall(mcp, state, wall=post)
        fire_and_forget(
            state.relay.set_wall(post), logger=_log, description="wall post"
        )

        remaining = format_remaining(post.expires_at)
        return f"Wall posted ({remaining}): {message}"
