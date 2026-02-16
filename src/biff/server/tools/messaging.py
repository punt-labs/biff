"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
``read_messages`` retrieves all unread messages and marks them read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import Message
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register messaging tools."""

    @mcp.tool(
        name="write",
        description=(
            "Send a message to a teammate. "
            "Messages are delivered to their inbox asynchronously."
        ),
    )
    async def write(to: str, message: str) -> str:
        """Send a message to another user's inbox, like BSD ``write(1)``."""
        await update_current_session(state)
        bare = to.strip().lstrip("@")
        msg = Message(
            from_user=state.config.user,
            to_user=bare,
            body=message,
        )
        await state.relay.deliver(msg)
        await refresh_read_messages(mcp, state)
        return f"Message sent to @{bare}."

    @mcp.tool(
        name="read_messages",
        description="Check your inbox for new messages. Marks all as read.",
    )
    async def read_messages() -> str:
        """Retrieve unread messages and mark them as read.

        Output mimics BSD ``from(1)``::

            From kai  Sun Feb 15 14:01  hey, ready for review?
            From eric Sun Feb 15 13:45  pushed the fix
        """
        await update_current_session(state)
        unread = await state.relay.fetch(state.config.user)
        if not unread:
            await refresh_read_messages(mcp, state)
            return "No new messages."
        await state.relay.mark_read(state.config.user, [m.id for m in unread])
        await refresh_read_messages(mcp, state)
        lines = [
            f"From {m.from_user:<8s} {m.timestamp.strftime('%a %b %d %H:%M')}  {m.body}"
            for m in unread
        ]
        return "\n".join(lines)
