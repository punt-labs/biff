"""Real-time conversation tools — ``talk``, ``talk_listen``, ``talk_end``.

``/talk`` initiates a real-time conversation with a teammate or agent.
``/talk_listen`` blocks until a message arrives or times out.
``/talk_end`` closes the conversation.

Talk uses NATS core pub/sub for instant message notification.  When
``/write`` delivers a message, :class:`~biff.nats_relay.NatsRelay`
publishes a JSON notification on a core NATS subject carrying the
sender and message body.  The background poller in ``_descriptions.py``
subscribes to these notifications and writes the latest talk message
to the unread status file so the status bar displays it within 0-2s.

``talk_listen`` still exists for agent-to-agent conversations where
blocking is appropriate.

Talk is NATS-only.  LocalRelay and DormantRelay return an error message.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.relay import Relay
from biff.server.tools._activate import auto_enable
from biff.server.tools._descriptions import (
    TALK_BASE_DESCRIPTION,
    get_talk_partner,
    refresh_read_messages,
    set_talk_partner,
)
from biff.server.tools._session import resolve_session, update_current_session
from biff.server.tools._tasks import fire_and_forget
from biff.tty import build_session_key, parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_NO_MESSAGES = "No new messages. Still listening."


def _reset_talk() -> None:
    """Clear talk state — test isolation."""
    set_talk_partner(None)


def format_talk_messages(messages: list[Message]) -> str:
    """Format messages in chat style for talk output."""
    lines: list[str] = []
    for m in messages:
        ts = m.timestamp.strftime("%H:%M:%S")
        lines.append(f"[{ts}] @{m.from_user}: {m.body}")
    return "\n".join(lines)


async def fetch_all_unread(
    relay: NatsRelay, session_key: str, user: str
) -> list[Message]:
    """Fetch and merge unread messages from both inboxes, sorted by time."""
    tty_unread = await relay.fetch(session_key)
    user_unread = await relay.fetch_user_inbox(user)
    return sorted(tty_unread + user_unread, key=lambda m: m.timestamp)


async def _resolve_talk_target(
    relay: Relay, user: str, tty: str | None
) -> tuple[str, str]:
    """Resolve a talk address to ``(relay_key, display_target)``.

    Same resolution as :func:`messaging._resolve_recipient` — tries the
    literal hex key first, then falls back to ``tty_name`` matching.
    """
    if tty:
        session = await resolve_session(relay, user, tty)
        if session:
            relay_key = build_session_key(session.user, session.tty)
        else:
            relay_key = f"{user}:{tty}"
        return relay_key, f"{user}:{tty}"
    return user, user


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
        all_unread = await fetch_all_unread(relay, session_key, user)
        if all_unread:
            await refresh_read_messages(mcp, state)
            return format_talk_messages(all_unread)

        # No messages — wait for notification.
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            return _NO_MESSAGES

        # Notification received — fetch messages.
        all_unread = await fetch_all_unread(relay, session_key, user)
        if not all_unread:
            return _NO_MESSAGES

        await refresh_read_messages(mcp, state)
        return format_talk_messages(all_unread)
    finally:
        await sub.unsubscribe()


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register talk tools."""

    @mcp.tool(
        name="talk",
        description=TALK_BASE_DESCRIPTION,
    )
    @auto_enable(state)
    async def talk(to: str, message: str = "") -> str:
        """Initiate a talk session.

        ``to`` is an address like ``@user`` or ``@user:tty``.
        An optional opening message is sent immediately.
        Once started, incoming messages from the partner appear on
        the status bar within 0-2s.  Use ``/write`` to reply.
        """
        relay = state.relay
        if not isinstance(relay, NatsRelay):
            return "Talk requires a NATS relay connection."

        await update_current_session(state)
        user, tty = parse_address(to)

        sessions = await relay.get_sessions_for_user(user)
        if not sessions:
            return f"@{user} is not online."

        relay_key, display_target = await _resolve_talk_target(relay, user, tty)
        set_talk_partner(display_target)

        if message:
            msg = Message(
                from_user=state.config.user,
                to_user=relay_key,
                body=message[:512],
            )
            await refresh_read_messages(mcp, state)
            fire_and_forget(
                relay.deliver(msg, sender_key=state.session_key),
                logger=logger,
                description="talk delivery",
            )

        return (
            f"Talk session started with @{display_target}. "
            f"Replies appear on the status bar. Use /write to reply."
        )

    @mcp.tool(
        name="talk_listen",
        description=(
            "Block until a message arrives (agent-to-agent only). "
            "Human sessions should NOT call this — incoming messages "
            "appear on the status bar automatically after /talk."
        ),
    )
    @auto_enable(state)
    async def talk_listen(timeout: int = 30) -> str:
        """Block until a message arrives or timeout expires.

        For agent-to-agent conversations where status bar display
        is not available.  Human sessions use status bar auto-read
        after ``/talk`` — do not call ``talk_listen`` in that case.
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
        partner = get_talk_partner()

        if partner is None:
            return "No active talk session."

        set_talk_partner(None)
        return f"Talk session with @{partner} ended."
