"""Async messaging tools — ``write`` and ``read_messages``.

``write`` delivers a message to another user's inbox, like BSD ``write(1)``.
Supports ``user`` (broadcast to all sessions) and ``user:tty`` (targeted).
``read_messages`` retrieves all unread messages for this session and marks
them read.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Coroutine
from typing import TYPE_CHECKING, Any

from biff.chunking import chunk_message
from biff.commands._messaging import deliver_with_retry
from biff.formatting import format_read
from biff.models import Message
from biff.server.tools._activity import track_activity
from biff.server.tools._descriptions import get_tty_name, refresh_read_messages
from biff.server.tools._session import get_or_create_session, update_current_session
from biff.server.tools._write_target import WriteRouter
from biff.tty import parse_address

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


_log = logging.getLogger(__name__)

_UnreadFetch = tuple[list[Message], list[Message]]

# Fetch-step labels, in the order results are unpacked into _UnreadFetch.
_TTY, _USER = "your tty inbox", "your broadcast inbox"


async def _fetch_unread_with_retry(
    state: ServerState,
) -> tuple[_UnreadFetch, str | None]:
    """Fetch unread messages, retrying only inboxes that failed.

    Each successful NATS pull destructively acknowledges messages. Retrying
    that inbox would lose its first result, so retain successful batches and
    retry only failed inboxes once. Return both fetched batches and a warning
    naming any inbox still unavailable, rather than misreporting it as empty.
    """
    steps: dict[str, Callable[[], Coroutine[Any, Any, list[Message]]]] = {
        _TTY: lambda: state.relay.fetch(state.session_key),
        _USER: lambda: state.relay.fetch_user_inbox(state.config.user),
    }

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

    fetched: _UnreadFetch = (results.get(_TTY, []), results.get(_USER, []))
    warning: str | None = None
    if pending:
        _log.warning(
            "read_messages could not check %s after retry: %s",
            ", ".join(pending),
            type(last_exc).__name__,
        )
        warning = (
            f"Could not check {', '.join(pending)} — failed twice. "
            "State unknown for that inbox, not confirmed empty."
        )
    return fetched, warning


async def _best_effort(update: Awaitable[object]) -> None:
    """Await a read_messages presence *update*; a failure is logged, never raised.

    A presence write must not stop the inbox fetch (and its retry), and after
    the fetch NATS has consumed the mail, so raising would lose it.
    """
    try:
        await update
    except Exception:  # noqa: BLE001 — tool boundary; the read must still succeed
        _log.warning("read_messages: presence update failed", exc_info=True)


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
        """Deliver a durable message to a teammate.

        Args:
            to: A bare username broadcasts to their shared inbox; user:tty targets
                a session. An @ prefix is optional.
            message: Text split into bounded chunks when needed.

        Returns:
            Confirmed delivery details, an addressing error, or an explicit failure
            after one retry. Delivery failures are not reported as success.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        await update_current_session(state)
        try:
            user, tty = parse_address(to)
        except ValueError as exc:
            return str(exc)
        try:
            target = await WriteRouter(
                local_repo=state.config.repo_name, visible_repos=state.visible_repos
            ).find(state.relay, user, tty)
        except ValueError as exc:
            _log.warning(
                "Write recipient could not be resolved (repos=%d)",
                len(state.visible_repos),
            )
            return str(exc)
        chunks = chunk_message(message)
        await refresh_read_messages(mcp, state)
        delivery = await deliver_with_retry(
            state.relay,
            from_user=state.config.user,
            from_tty=get_tty_name(),
            session_key=state.session_key,
            to_user=target.relay_key,
            target_repo=target.target_repo,
            chunks=chunks,
            display=target.display,
        )
        return target.report(delivery)

    @mcp.tool(
        name="read_messages",
        description="Check your inbox for new messages. Marks all as read.",
        meta={"anthropic/alwaysLoad": True},
    )
    @track_activity(state)
    async def read_messages() -> str:
        """Consume unread durable messages for the current session.

        Call without arguments after an inbox notification. Merge the targeted
        session inbox and shared user inbox in chronological order. The first
        session to read consumes broadcast messages; they are not replayed.

        Returns:
            Formatted messages or 'No new messages.'. A failed inbox is retried
            once, then named in a warning alongside any successful inbox results;
            a failed fetch is never described as a confirmed-empty inbox.

        Raises:
            OSError: Local relay storage cannot be accessed.
            ConnectionError: The network relay cannot establish a connection.
            TimeoutError: A relay operation exceeds its deadline.
            nats.errors.Error: A NATS request or connection fails.
        """
        session_key = state.session_key
        user = state.config.user
        # Mail routing reads last_tool_at, so an empty poll is not work; it only
        # keeps the presence row alive past its TTL.
        await _best_effort(get_or_create_session(state))

        (tty_unread, user_unread), warning = await _fetch_unread_with_retry(state)

        all_unread = sorted(tty_unread + user_unread, key=lambda m: m.timestamp)

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

        await refresh_read_messages(mcp, state)
        await _best_effort(update_current_session(state))

        result = format_read(all_unread)
        return f"{warning}\n\n{result}" if warning is not None else result
