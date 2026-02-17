"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
Supports ``@user`` (broadcast to all sessions) and ``@user:tty`` (targeted).
``read_messages`` retrieves all unread messages for this session and marks
them read.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.models import Message
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import resolve_session, update_current_session
from biff.tty import build_session_key, parse_address

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
        """Send a message to another user's inbox, like BSD ``write(1)``.

        ``@user`` broadcasts to all sessions of that user.
        ``@user:tty`` targets a specific session.
        """
        await update_current_session(state)
        user, tty = parse_address(to)
        if tty:
            # Resolve tty_name to actual session key
            session = await resolve_session(state.relay, user, tty)
            if session:
                to_user = build_session_key(session.user, session.tty)
            else:
                to_user = f"{user}:{tty}"
        else:
            to_user = user
        msg = Message(
            from_user=state.config.user,
            to_user=to_user,
            body=message,
        )
        await state.relay.deliver(msg)
        await refresh_read_messages(mcp, state)
        display = f"@{user}:{tty}" if tty else f"@{user}"
        return f"Message sent to {display}."

    @mcp.tool(
        name="read_messages",
        description="Check your inbox for new messages. Marks all as read.",
    )
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
        from_w = max(4, max(len(m.from_user) for m in all_unread))
        header = f"\u25b6  {'FROM':<{from_w}}  {'DATE':<16}  MESSAGE"
        lines: list[str] = []
        for m in all_unread:
            ts = m.timestamp.strftime("%a %b %d %H:%M")
            lines.append(f"   {m.from_user:<{from_w}}  {ts:<16}  {m.body}")
        return header + "\n" + "\n".join(lines)
