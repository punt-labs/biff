"""Dynamic tool description updates and inbox polling.

Refreshes tool descriptions based on current server state.
Called after every tool execution (belt) and by a background
poller (suspenders) so notifications stay fresh even between
tool calls.

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

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

logger = logging.getLogger(__name__)

_CHECK_MESSAGES_BASE = "Check your inbox for new messages. Marks all as read."

_DEFAULT_POLL_INTERVAL = 2.0


def refresh_check_messages(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``check_messages`` tool description with unread count.

    When the user has unread messages, the description changes to show
    the count and a preview, e.g.::

        Check messages (2 unread: @kai about auth, @eric about lunch).
        Marks all as read.

    When the inbox is empty, the description reverts to the base text.

    If ``state.unread_path`` is set, also writes the unread summary to
    a JSON file for status bar consumption.
    """
    tool = mcp._tool_manager._tools.get("check_messages")  # pyright: ignore[reportPrivateUsage]
    if tool is None:
        return
    summary = state.messages.get_unread_summary(state.config.user)
    if summary.count == 0:
        tool.description = _CHECK_MESSAGES_BASE
    else:
        tool.description = (
            f"Check messages ({summary.count} unread: {summary.preview}). "
            "Marks all as read."
        )
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

    In Phase 2 the relay will push notifications directly, replacing
    the polling loop. The refresh mechanism stays the same.
    """
    last_count = -1  # Force initial refresh
    while True:
        await asyncio.sleep(interval)
        summary = state.messages.get_unread_summary(state.config.user)
        if summary.count != last_count:
            last_count = summary.count
            refresh_check_messages(mcp, state)


def _write_unread_file(path: Path, summary: UnreadSummary) -> None:
    """Atomically write unread summary to a JSON file.

    Uses the same temp-file-then-rename pattern as the storage layer.
    Failures are logged but never propagated — tool execution must not
    break because a status file could not be written.
    """
    tmp = path.with_suffix(".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {"count": summary.count, "preview": summary.preview}
        tmp.write_text(json.dumps(data, indent=2) + "\n")
        tmp.replace(path)
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
        tmp.unlink(missing_ok=True)
