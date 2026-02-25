"""Real-time conversation tools — ``talk``, ``talk_listen``, ``talk_end``.

``/talk`` initiates a real-time conversation with a teammate or agent.
``/talk_listen`` blocks until a message arrives or times out.
``/talk_end`` closes the conversation.

Talk uses NATS core pub/sub for instant message notification.  When
``/write`` delivers a message, :class:`~biff.nats_relay.NatsRelay`
publishes a lightweight notification on a core NATS subject.  A blocking
``talk_listen`` call subscribes to that subject and wakes immediately
when notified, then fetches messages from the inbox.

Talk is NATS-only.  LocalRelay and DormantRelay return an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import refresh_read_messages
from biff.server.tools._session import update_current_session
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_NO_MESSAGES = "No new messages. Still listening."

# Module-level talk state (per-process, like _tty_name in _descriptions.py).
_talk_partner: str | None = None


def _reset_talk() -> None:
    """Clear talk state — test isolation."""
    global _talk_partner
    _talk_partner = None


def _format_talk_messages(messages: list[Message]) -> str:
    """Format messages in chat style for talk output."""
    lines: list[str] = []
    for m in messages:
        ts = m.timestamp.strftime("%H:%M:%S")
        lines.append(f"[{ts}] @{m.from_user}: {m.body}")
    return "\n".join(lines)


async def _fetch_all_unread(
    relay: NatsRelay, session_key: str, user: str
) -> list[Message]:
    """Fetch and merge unread messages from both inboxes, sorted by time."""
    tty_unread = await relay.fetch(session_key)
    user_unread = await relay.fetch_user_inbox(user)
    return sorted(tty_unread + user_unread, key=lambda m: m.timestamp)


async def _do_talk_listen(
    mcp: FastMCP[ServerState],
    state: ServerState,
    relay: NatsRelay,
    timeout: float,
) -> str:
    """Core talk_listen logic — subscribe, check inbox, block, return messages."""
    user = state.config.user
    session_key = state.session_key

    nc = await relay.get_nc()
    subject = relay.talk_notify_subject(user)

    # Subscribe FIRST to avoid missing notifications during fetch.
    event = asyncio.Event()

    async def _on_notify(_msg: object) -> None:
        event.set()

    sub = await nc.subscribe(  # pyright: ignore[reportUnknownMemberType]
        subject, cb=_on_notify
    )
    try:
        # Check for existing unread messages.
        all_unread = await _fetch_all_unread(relay, session_key, user)
        if all_unread:
            await refresh_read_messages(mcp, state)
            return _format_talk_messages(all_unread)

        # No messages — wait for notification.
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return _NO_MESSAGES

        # Notification received — fetch messages.
        all_unread = await _fetch_all_unread(relay, session_key, user)
        if not all_unread:
            return _NO_MESSAGES

        await refresh_read_messages(mcp, state)
        return _format_talk_messages(all_unread)
    finally:
        await sub.unsubscribe()


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register talk tools."""

    @mcp.tool(
        name="talk",
        description=(
            "Start a real-time conversation with a teammate or agent. "
            "Use talk_listen to wait for replies. Use talk_end to close."
        ),
    )
    @auto_enable(state)
    async def talk(to: str, message: str = "") -> str:
        """Initiate a talk session.

        ``to`` is an address like ``@user`` or ``@user:tty``.
        An optional opening message is sent immediately.
        After starting, use ``talk_listen`` to wait for replies
        and ``write`` to send messages.
        """
        global _talk_partner

        relay = state.relay
        if not isinstance(relay, NatsRelay):
            return "Talk requires a NATS relay connection."

        await update_current_session(state)
        user, _tty = parse_address(to)

        sessions = await relay.get_sessions_for_user(user)
        if not sessions:
            return f"@{user} is not online."

        _talk_partner = user

        if message:
            msg = Message(
                from_user=state.config.user,
                to_user=user,
                body=message[:512],
            )
            await relay.deliver(msg)
            await refresh_read_messages(mcp, state)

        return (
            f"Talk session started with @{user}. Use talk_listen to wait for replies."
        )

    @mcp.tool(
        name="talk_listen",
        description=(
            "Wait for the next message in a conversation. "
            "Blocks until a message arrives or times out. "
            "Call repeatedly in a loop to maintain the conversation."
        ),
    )
    @auto_enable(state)
    async def talk_listen(timeout: int = 30) -> str:
        """Block until a message arrives or timeout expires.

        Subscribes to NATS core notifications, checks the inbox for
        existing messages, and blocks if empty.  Returns all unread
        messages on wake-up.

        Call repeatedly in a loop to maintain a talk session::

            talk @user "hello"
            talk_listen  → messages
            write @user "reply"
            talk_listen  → more messages
            talk_end
        """
        relay = state.relay
        if not isinstance(relay, NatsRelay):
            return "Talk requires a NATS relay connection."

        await update_current_session(state)
        return await _do_talk_listen(mcp, state, relay, float(timeout))

    @mcp.tool(
        name="talk_end",
        description="End the current talk session.",
    )
    @auto_enable(state)
    async def talk_end() -> str:
        """Close the active talk session."""
        global _talk_partner

        if _talk_partner is None:
            return "No active talk session."

        partner = _talk_partner
        _talk_partner = None
        return f"Talk session with @{partner} ended."
