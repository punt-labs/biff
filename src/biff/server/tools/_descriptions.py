"""Dynamic tool description updates.

Refreshes tool descriptions based on current server state.
Called after every tool execution so that the next ``tools/list``
response reflects the latest state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState

_CHECK_MESSAGES_BASE = "Check your inbox for new messages. Marks all as read."


def refresh_check_messages(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Update the ``check_messages`` tool description with unread count.

    When the user has unread messages, the description changes to show
    the count and a preview, e.g.::

        Check messages (2 unread: @kai about auth, @eric about lunch).
        Marks all as read.

    When the inbox is empty, the description reverts to the base text.
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
