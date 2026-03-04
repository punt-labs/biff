"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
Supports ``@user`` (broadcast to all sessions) and ``@user:tty`` (targeted).
``read_messages`` retrieves all unread messages for this session and marks
them read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.formatting import format_read
from biff.models import Message
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import resolve_session, update_current_session
from biff.tty import build_session_key, parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


async def _resolve_recipient(state: ServerState, to: str) -> tuple[str, str]:
    """Resolve an address to ``(relay_key, display_name)``."""
    user, tty = parse_address(to)
    if tty:
        session = await resolve_session(state.relay, user, tty)
        if session:
            relay_key = build_session_key(session.user, session.tty)
        else:
            relay_key = f"{user}:{tty}"
    else:
        relay_key = user
    display = f"@{user}:{tty}" if tty else f"@{user}"
    return relay_key, display


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register messaging tools."""

    @mcp.tool(
        name="write",
        description=(
            "Send a message to a teammate. "
            "Messages are delivered to their inbox asynchronously."
        ),
    )
    @auto_enable(state)
    async def write(to: str, message: str) -> str:
        """Send a message to another user's inbox, like BSD ``write(1)``.

        ``@user`` broadcasts to all sessions of that user.
        ``@user:tty`` targets a specific session.
        """
        await update_current_session(state)
        to_user, display = await _resolve_recipient(state, to)
        msg = Message(
            from_user=state.config.user,
            to_user=to_user,
            body=message[:512],
        )
        await state.relay.deliver(msg, sender_key=state.session_key)
        await refresh_read_messages(mcp, state)
        return f"Message sent to {display}."

    @mcp.tool(
        name="read_messages",
        description="Check your inbox for new messages. Marks all as read.",
    )
    @auto_enable(state)
    async def read_messages() -> str:
        """Retrieve unread messages and mark them as read.

        Merges the per-user broadcast mailbox and the per-TTY targeted
        inbox into a single chronological view.  POP semantics apply
        independently to each — the first session to ``/read`` consumes
        broadcast messages.

        Output mimics BSD ``from(1)``::

            From kai  Sun Feb 15 14:01  hey, ready for review?
            From eric Sun Feb 15 13:45  pushed the fix
        """
        await update_current_session(state)
        session_key = state.session_key
        user = state.config.user

        # Fetch from both inboxes
        tty_unread = await state.relay.fetch(session_key)
        user_unread = await state.relay.fetch_user_inbox(user)
        all_unread = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)

        if not all_unread:
            await refresh_read_messages(mcp, state)
            return "No new messages."

        # Mark read independently in each inbox
        tty_ids = [m.id for m in tty_unread]
        user_ids = [m.id for m in user_unread]
        if tty_ids:
            await state.relay.mark_read(session_key, tty_ids)
        if user_ids:
            await state.relay.mark_read_user_inbox(user, user_ids)

        await refresh_read_messages(mcp, state)
        return format_read(all_unread)
