"""Biff's FastMCP transport adapter."""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastmcp import FastMCP
from mcp.server.subscriptions import ListenHandler
from mcp.types import SubscriptionsListenRequestParams

from biff.server.caller_middleware import CallerIdentityMiddleware

if TYPE_CHECKING:
    from fastmcp.server.middleware import Middleware
    from mcp.server.subscriptions import SubscriptionBus

    from biff.server.caller_policy import CallerPolicy


class NotificationServer[LifespanT](FastMCP[LifespanT]):
    """FastMCP server with MCP 2026-07-28 tool list subscriptions."""

    def configure_transport(
        self, bus: SubscriptionBus, handshake: Middleware, policy: CallerPolicy
    ) -> None:
        """Register Biff's subscription and middleware handlers.

        Args:
            bus: The local event bus used by Biff's description refreshes.
            handshake: Middleware to capture the handshake-era session.
            policy: Caller validation and session binding for tool calls.
        """
        self._mcp_server.add_request_handler(
            "subscriptions/listen", SubscriptionsListenRequestParams, ListenHandler(bus)
        )
        self.add_middleware(handshake)
        self.add_middleware(CallerIdentityMiddleware(policy))
