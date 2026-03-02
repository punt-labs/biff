"""Notification tracking for biff MCP integration tests.

Reusable ``MessageHandler`` that counts ``tools/list_changed``
notifications.  Works across all test tiers — integration,
NATS E2E, and subprocess — wherever a ``fastmcp.Client``
accepts a ``message_handler``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp.client.messages import MessageHandler

if TYPE_CHECKING:
    from mcp import types as mcp_types


class NotificationTracker(MessageHandler):
    """Message handler that counts ``tools/list_changed`` notifications."""

    def __init__(self) -> None:
        self.tool_list_changed_count = 0

    async def on_tool_list_changed(
        self,
        message: mcp_types.ToolListChangedNotification,  # noqa: ARG002
    ) -> None:
        self.tool_list_changed_count += 1
