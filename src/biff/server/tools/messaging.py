"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
Supports ``user`` (broadcast to all sessions) and ``user:tty`` (targeted).
``read_messages`` retrieves all unread messages for this session and marks
them read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Coroutine
from typing import TYPE_CHECKING, Any

from biff.chunking import chunk_message
from biff.commands._messaging import deliver_with_retry
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

_UnreadFetch = tuple[list[Message], list[Message], list[Message], list[Message]]

# Fetch-step labels, in the order results are unpacked into _UnreadFetch.
_TTY, _USER, _COMP_TTY, _COMP_USER = (
    "your tty inbox",
    "your broadcast inbox",
    "companion tty inbox",
    "companion broadcast inbox",
)


async def _fetch_unread_with_retry(
    state: ServerState,
) -> tuple[_UnreadFetch, str | None]:
    """Fetch unread messages, retrying only the inboxes that failed.

    Returns ``(fetched, warning)``. Each of the (up to four) inboxes is
    fetched independently and retried at most once on its own — NEVER as
    part of restarting the whole batch. ``NatsRelay.fetch``/
    ``fetch_user_inbox`` ack (destructively delete) messages from the
    stream as a side effect of a successful pull; retrying the whole batch
    after one inbox already succeeded would re-fetch an inbox that has
    nothing left to return, silently discarding the messages the first
    attempt already pulled (review finding on this branch — the retry
    added to fix biff-brn's indistinguishability problem introduced a
    worse, silent data-loss failure mode of its own).

    An inbox still failing after its own retry is named in ``warning``
    rather than rendered as empty; its slot in ``fetched`` stays empty
    because nothing was lost there — nothing was successfully acked from
    it either. ``fetched`` always reflects every inbox that DID succeed,
    even when another inbox's warning is also present, so a partial
    failure never costs the caller messages that were already pulled.
    """
    companion = state.companion
    steps: dict[str, Callable[[], Coroutine[Any, Any, list[Message]]]] = {
        _TTY: lambda: state.relay.fetch(state.session_key),
        _USER: lambda: state.relay.fetch_user_inbox(state.config.user),
    }
    if companion is not None:
        steps[_COMP_TTY] = lambda: state.relay.fetch(companion.session_key)
        steps[_COMP_USER] = lambda: state.relay.fetch_user_inbox(companion.user)

    results: dict[str, list[Message]] = {}
    pending = list(steps)
    last_exc: Exception | None = None
    for _attempt in range(2):  # first pass + one retry, per still-pending step only
        if not pending:
            break
        still_pending: list[str] = []
        for label in pending:
            try:
                results[label] = await steps[label]()
            except Exception as exc:  # noqa: BLE001 — retried once per inbox below
                last_exc = exc
                still_pending.append(label)
        pending = still_pending

    fetched: _UnreadFetch = (
        results.get(_TTY, []),
        results.get(_USER, []),
        results.get(_COMP_TTY, []),
        results.get(_COMP_USER, []),
    )
    warning: str | None = None
    if pending:
        _log.warning(
            "read_messages could not check %s after retry: %s",
            ", ".join(pending),
            last_exc,
            exc_info=last_exc,
        )
        warning = (
            f"Could not check {', '.join(pending)} — failed twice. "
            "State unknown for that inbox, not confirmed empty."
        )
    return fetched, warning


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
            "Delivery is confirmed before this returns; a failure after "
            "one retry is reported explicitly, never silently."
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
        return await deliver_with_retry(
            state.relay,
            from_user=state.config.user,
            from_tty=get_tty_name(),
            session_key=state.session_key,
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

        (
            (tty_unread, user_unread, comp_tty, comp_user),
            warning,
        ) = await _fetch_unread_with_retry(state)

        all_unread = sorted(
            tty_unread + user_unread + comp_tty + comp_user,
            key=lambda m: m.timestamp,
        )

        if not all_unread:
            if warning is not None:
                return warning
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
            result = format_read_dual(
                state.companion.user,
                human_msgs,
                state.config.user,
                agent_msgs,
            )
        else:
            result = format_read(all_unread)
        return f"{warning}\n\n{result}" if warning is not None else result
