"""Team broadcast tool — ``wall``.

``/wall`` posts a time-limited banner visible to all teammates on their
status bar and tool descriptions.  Unlike ``/write``, wall messages do
not go into inboxes and do not require ``/read``.

BSD ``wall(1)`` wrote to every logged-in terminal.  Biff adds
duration-based persistence and explicit clearing.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from pydantic import ValidationError

from biff.models import WallPost
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_wall
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")
_DURATION_UNITS: dict[str, int] = {"m": 60, "h": 3600, "d": 86400}
_MAX_DURATION = timedelta(days=3)  # Capped by sessions KV bucket TTL
_DEFAULT_DURATION = timedelta(hours=1)

WALL_BASE_DESCRIPTION = (
    "Broadcast a message to the whole team. "
    "The message appears on every teammate's status bar and tool list. "
    "Use clear=True to remove an active wall."
)


def _parse_duration(s: str) -> timedelta:
    """Parse a human duration string like ``30m``, ``2h``, ``1d``.

    Returns :data:`_DEFAULT_DURATION` (1 hour) when *s* is empty.
    """
    s = s.strip().lower()
    if not s:
        return _DEFAULT_DURATION
    if len(s) < 2 or s[-1] not in _DURATION_UNITS:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg)
    try:
        n = int(s[:-1])
    except ValueError:
        msg = f"Unrecognized duration {s!r}. Use 30m, 2h, 1d, 3d."
        raise ValueError(msg) from None
    if n <= 0:
        msg = "Duration must be positive."
        raise ValueError(msg)
    try:
        td = timedelta(seconds=n * _DURATION_UNITS[s[-1]])
    except OverflowError:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg) from None
    if td > _MAX_DURATION:
        msg = f"Duration {s!r} exceeds maximum of 3 days."
        raise ValueError(msg)
    return td


def format_remaining(expires_at: datetime) -> str:
    """Human-readable time remaining until *expires_at*."""
    remaining = expires_at - datetime.now(UTC)
    total_seconds = int(remaining.total_seconds())
    if total_seconds <= 0:
        return "expired"
    hours, remainder = divmod(total_seconds, 3600)
    minutes = remainder // 60
    if hours > 0:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{minutes}m" if minutes else "<1m"


def _format_wall(wall: WallPost) -> str:
    """Format a wall post for display."""
    remaining = format_remaining(wall.expires_at)
    sender = f"@{wall.from_user}"
    if wall.from_tty:
        sender += f" ({wall.from_tty})"
    return f"\u25b6  WALL from {sender} ({remaining} remaining)\n   {wall.text}"


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
            await state.relay.set_wall(None)
            await refresh_wall(mcp, state)
            return "Wall cleared."

        # Sanitize: strip control chars, collapse to single line
        message = _CTRL_RE.sub("", message)
        message = " ".join(message.split())
        if not message:
            current = await state.relay.get_wall()
            if current is None:
                await refresh_wall(mcp, state, wall=None)
                return "No active wall."
            return _format_wall(current)

        # Post mode
        try:
            ttl = _parse_duration(duration)
        except ValueError as exc:
            return str(exc)

        now = datetime.now(UTC)
        message = message[:512]
        try:
            post = WallPost(
                text=message,
                from_user=state.config.user,
                from_tty=session.tty_name,
                posted_at=now,
                expires_at=now + ttl,
            )
        except ValidationError as exc:
            for err in exc.errors():
                if err.get("type") == "string_too_short":
                    return "Message cannot be blank."
            return str(exc)
        await state.relay.set_wall(post)
        await refresh_wall(mcp, state)

        remaining = format_remaining(post.expires_at)
        return f"Wall posted ({remaining}): {message}"
