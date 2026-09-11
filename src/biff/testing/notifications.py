"""Notification tracking for biff MCP integration tests.

Reusable ``MessageHandler`` that counts and times ``tools/list_changed``
notifications.  Works across all test tiers — integration, NATS E2E, and
subprocess — wherever a ``fastmcp.Client`` accepts a ``message_handler``.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from fastmcp.client.messages import MessageHandler

if TYPE_CHECKING:
    from mcp import types as mcp_types


class NotificationTracker(MessageHandler):
    """Message handler that counts and times ``tools/list_changed`` notifications.

    Each received notification appends a ``time.monotonic()`` reading to
    :attr:`tool_list_changed_at`, so a caller can measure delivery latency
    (arrival time of the push) separately from the slower
    description-mutation latency a caller measures by polling
    ``list_tools()`` afterward.
    """

    def __init__(self) -> None:
        self._tool_list_changed_at: list[float] = []

    @property
    def tool_list_changed_count(self) -> int:
        """How many ``tools/list_changed`` notifications have arrived.

        Derived from :attr:`tool_list_changed_at`'s length rather than a
        separately incremented counter — the two can never drift apart,
        because there is only one number to keep, not two kept in lockstep.
        """
        return len(self._tool_list_changed_at)

    @property
    def tool_list_changed_at(self) -> tuple[float, ...]:
        """``time.monotonic()`` reading recorded for each notification received."""
        return tuple(self._tool_list_changed_at)

    async def on_tool_list_changed(
        self,
        message: mcp_types.ToolListChangedNotification,  # noqa: ARG002
    ) -> None:
        self._tool_list_changed_at.append(time.monotonic())
