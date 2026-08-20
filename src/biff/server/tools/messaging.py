"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
Supports ``user`` (broadcast to all sessions) and ``user:tty`` (targeted).
``read_messages`` retrieves all unread messages for this session and marks
them read.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from biff.chunking import chunk_message
from biff.formatting import format_read, format_read_dual
from biff.models import Message
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import get_tty_name, refresh_read_messages
from biff.server.tools._session import resolve_tty_name, update_current_session
from biff.tty import build_session_key, parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


async def _resolve_recipient(
    state: ServerState, to: str
) -> tuple[str, str, str | None]:
    """Resolve an address to ``(relay_key, display_name, target_repo)``.

    For targeted addresses (``user:tty``), searches sessions across all
    visible repos.  When the resolved session is in a different repo,
    *target_repo* is set for cross-repo delivery.  Bare ``user``
    addresses stay repo-local (``target_repo=None``).
    """
    user, tty = parse_address(to)
    target_repo: str | None = None
    if tty:
        # Search across visible repos for the target session.
        all_sessions = await state.relay.get_sessions_for_repos(state.visible_repos)
        session = resolve_tty_name(
            all_sessions, user, tty, local_repo=state.config.repo_name
        )
        if session:
            relay_key = build_session_key(session.user, session.tty)
            if (
                session.repo
                and session.repo != state.config.repo_name
                and session.repo in state.visible_repos
            ):
                target_repo = session.repo
        else:
            user_exists = any(s.user == user for s in all_sessions)
            if user_exists:
                msg = (
                    f"No active session {user}:{tty}."
                    " Run /who to find their current address."
                )
            else:
                msg = f"User {user} not found in visible repos."
            _log.warning(
                "Write failed: %r (visible_repos=%s, sessions=%d)",
                msg,
                sorted(state.visible_repos),
                len(all_sessions),
            )
            raise ValueError(msg)
    else:
        relay_key = user
    display = f"{user}:{tty}" if tty else user
    return relay_key, display, target_repo


_log = logging.getLogger(__name__)


async def _deliver_with_retry(
    state: ServerState,
    *,
    to_user: str,
    target_repo: str | None,
    chunks: list[str],
    display: str,
) -> str:
    """Deliver chunks, retrying once from the first undelivered chunk on failure.

    Returns the user-facing result string. Delivery is awaited here, not
    fire-and-forget: the prior fire-and-forget design returned "Message
    sent" before delivery was even attempted, so a publish failure —
    surfaced only as a background log line nobody reads — left the sender
    believing a message had arrived that never did, with no error on
    either end and no recovery path (biff-0px). On failure, retry once
    from the first undelivered chunk rather than restarting from the
    first chunk, so a partial failure cannot redeliver an already-sent
    one — mirroring the recovery pattern observed for read_messages
    (biff-brn: every session-reported occurrence cleared on the very next
    attempt).
    """

    async def _deliver_one(chunk: str) -> None:
        msg = Message(
            from_user=state.config.user,
            from_tty=get_tty_name(),
            to_user=to_user,
            body=chunk,
        )
        await state.relay.deliver(
            msg, sender_key=state.session_key, target_repo=target_repo
        )

    delivered = 0
    try:
        for chunk in chunks:
            await _deliver_one(chunk)
            delivered += 1
    except Exception:  # noqa: BLE001 — retry boundary, re-raised below on retry failure
        try:
            for chunk in chunks[delivered:]:
                await _deliver_one(chunk)
                delivered += 1
        except Exception as exc:  # noqa: BLE001 — MCP tool boundary, reported to caller
            _log.warning(
                "Message delivery to %s failed twice (delivered %d/%d parts): %s",
                display,
                delivered,
                len(chunks),
                exc,
                exc_info=exc,
            )
            return (
                f"Could not deliver to {display} — failed twice ({exc}). "
                f"{delivered}/{len(chunks)} part(s) confirmed sent; "
                "the rest may not have arrived. Not confirmed sent."
            )
    parts = len(chunks)
    suffix = f" ({parts} parts)" if parts > 1 else ""
    return f"Message sent to {display}.{suffix}"


async def _fetch_companion_unread(
    state: ServerState,
) -> tuple[list[Message], list[Message]]:
    """Fetch unread messages from the companion's inboxes.

    Returns ``(tty_unread, user_unread)`` — both empty when no companion.
    """
    companion = state.companion
    if companion is None:
        return [], []
    tty = await state.relay.fetch(companion.session_key)
    user = await state.relay.fetch_user_inbox(companion.user)
    return tty, user


async def _mark_companion_read(
    state: ServerState,
    tty_unread: list[Message],
    user_unread: list[Message],
) -> None:
    """Mark companion inbox messages as read."""
    companion = state.companion
    if companion is None:
        return
    tty_ids = [m.id for m in tty_unread]
    user_ids = [m.id for m in user_unread]
    if tty_ids:
        await state.relay.mark_read(companion.session_key, tty_ids)
    if user_ids:
        await state.relay.mark_read_user_inbox(companion.user, user_ids)


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register messaging tools."""

    @mcp.tool(
        name="write",
        description=(
            "Send a message to a teammate. "
            "Messages are delivered to their inbox asynchronously."
        ),
    )
    @track_activity(state)
    async def write(to: str, message: str) -> str:
        """Send a message to another user's inbox, like BSD ``write(1)``.

        ``user`` broadcasts to all sessions of that user.
        ``user:tty`` targets a specific session.
        """
        await update_current_session(state)
        try:
            to_user, display, target_repo = await _resolve_recipient(state, to)
        except ValueError as exc:
            return str(exc)
        chunks = chunk_message(message)
        await refresh_read_messages(mcp, state)
        return await _deliver_with_retry(
            state,
            to_user=to_user,
            target_repo=target_repo,
            chunks=chunks,
            display=display,
        )

    @mcp.tool(
        name="read_messages",
        description="Check your inbox for new messages. Marks all as read.",
        meta={"anthropic/alwaysLoad": True},
    )
    @track_activity(state)
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

        # Fetch from primary inboxes (per-TTY + per-user broadcast).
        tty_unread = await state.relay.fetch(session_key)
        user_unread = await state.relay.fetch_user_inbox(user)

        # Fetch from companion inboxes (DES-039).
        comp_tty, comp_user = await _fetch_companion_unread(state)

        all_unread = sorted(
            tty_unread + user_unread + comp_tty + comp_user,
            key=lambda m: m.timestamp,
        )

        if not all_unread:
            await refresh_read_messages(mcp, state)
            return "No new messages."

        # Mark read independently in each inbox.
        tty_ids = [m.id for m in tty_unread]
        user_ids = [m.id for m in user_unread]
        if tty_ids:
            await state.relay.mark_read(session_key, tty_ids)
        if user_ids:
            await state.relay.mark_read_user_inbox(user, user_ids)
        await _mark_companion_read(state, comp_tty, comp_user)

        await refresh_read_messages(mcp, state)

        if state.companion is not None:
            human_msgs = sorted(comp_tty + comp_user, key=lambda m: m.timestamp)
            agent_msgs = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)
            return format_read_dual(
                state.companion.user,
                human_msgs,
                state.config.user,
                agent_msgs,
            )
        return format_read(all_unread)
