"""Dynamic tool description updates.

Refreshes tool descriptions based on current server state.
Called after every tool execution so that the next ``tools/list``
response reflects the latest state.

Also writes an ``unread.json`` status file (when configured) so that
external tools like the Claude Code status bar can display a live
unread count without querying the MCP server.
"""

from __future__ import annotations

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
        tmp.rename(path)
    except OSError:
        logger.warning("Failed to write unread status file %s", path, exc_info=True)
        tmp.unlink(missing_ok=True)
