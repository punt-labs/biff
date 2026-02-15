"""Dynamic tool description updates and inbox polling.

Refreshes tool descriptions based on current server state.
Called after every tool execution (belt) and by a background
poller (suspenders) so notifications stay fresh even between
tool calls.

After mutating the ``check_messages`` description, fires
``notifications/tools/list_changed`` so Claude Code re-reads
the tool list and sees the updated unread count.

Also writes an ``unread.json`` status file (when configured) so that
external tools like the Claude Code status bar can display a live
unread count without querying the MCP server.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from biff.models import UnreadSummary
from biff.relay import atomic_write

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from mcp.server.session import ServerSession

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_CHECK_MESSAGES_BASE = "Check your inbox for new messages. Marks all as read."

_DEFAULT_POLL_INTERVAL = 2.0

# Captured from the first tool-handler context so the background
# poller can send notifications outside a request context.
_session: ServerSession | None = None


async def _notify_tool_list_changed() -> None:
    """Fire ``notifications/tools/list_changed`` via the best available path.

    Belt path (inside a tool handler): queues the notification on the
    FastMCP Context so it piggybacks on the tool response.

    Suspenders path (background poller): sends directly on the stored
    ServerSession when no request context is active.
    """
    global _session

    # Belt path — inside a tool handler, Context is available.
    try:
        from fastmcp.server.dependencies import get_context  # noqa: PLC0415

        ctx = get_context()
        ctx._queue_tool_list_changed()  # pyright: ignore[reportPrivateUsage]
        # Always update — the client may have reconnected with a new session.
        _session = ctx.session
        return
    except RuntimeError:
        pass

    # Suspenders path — no request context, use stored session.
    # Bare Exception matches FastMCP's own _flush_notifications pattern —
    # notification delivery is best-effort and must never crash the poller.
    if _session is not None:
        try:
            await _session.send_tool_list_changed()
        except Exception:  # noqa: BLE001
            logger.debug("Failed to send tool list changed notification", exc_info=True)


async def refresh_check_messages(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``check_messages`` tool description with unread count.

    When the user has unread messages, the description changes to show
    the count and a preview, e.g.::

        Check messages (2 unread: @kai about auth, @eric about lunch).
        Marks all as read.

    When the inbox is empty, the description reverts to the base text.

    After mutation, fires ``notifications/tools/list_changed`` so the
    client re-reads the tool list and sees the new description.

    If ``state.unread_path`` is set, also writes the unread summary to
    a JSON file for status bar consumption.
    """
    tool = mcp._tool_manager._tools.get("check_messages")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        return
    summary = await state.relay.get_unread_summary(state.config.user)
    old_desc = tool.description
    if summary.count == 0:
        tool.description = _CHECK_MESSAGES_BASE
    else:
        tool.description = (
            f"Check messages ({summary.count} unread: {summary.preview}). "
            "Marks all as read."
        )
    if tool.description != old_desc:
        await _notify_tool_list_changed()
    if state.unread_path is not None:
        _write_unread_file(state.unread_path, summary)


async def poll_inbox(
    mcp: FastMCP[ServerState],
    state: ServerState,
    *,
    interval: float = _DEFAULT_POLL_INTERVAL,
) -> None:
    """Background task: poll inbox and refresh notifications on change.

    Runs for the lifetime of the MCP server. Detects new or read
    messages by comparing the unread count against the last known
    value, then calls :func:`refresh_check_messages` to update both
    the tool description and the status file.

    The notification is sent via :func:`_notify_tool_list_changed`,
    which uses a stored ``ServerSession`` reference captured from
    the first tool call.

    In Phase 2 the relay will push notifications directly, replacing
    the polling loop. The refresh mechanism stays the same.
    """
    last_count = -1  # Force initial refresh
    while True:
        await asyncio.sleep(interval)
        summary = await state.relay.get_unread_summary(state.config.user)
        if summary.count != last_count:
            last_count = summary.count
            await refresh_check_messages(mcp, state)


def _write_unread_file(path: Path, summary: UnreadSummary) -> None:
    """Write unread summary to a JSON status file.

    Failures are logged but never propagated — tool execution must not
    break because a status file could not be written.
    """
    data = {"count": summary.count, "preview": summary.preview}
    try:
        atomic_write(path, json.dumps(data, indent=2) + "\n")
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
