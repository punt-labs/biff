"""Behavior of the MCP subscription transport adapter."""

from __future__ import annotations

import asyncio
from contextlib import nullcontext

from fastmcp.server.middleware import Middleware
from mcp import Client
from mcp.server.subscriptions import InMemorySubscriptionBus
from mcp.shared.subscriptions import ToolsListChanged

from biff.server.caller_policy import CallerPolicy
from biff.server.mcp_protocol import NotificationServer


async def test_subscription_uses_configured_bus() -> None:
    server = NotificationServer[None]("notification-test")
    bus = InMemorySubscriptionBus()
    server.configure_transport(
        bus, Middleware(), CallerPolicy(lambda _caller: nullcontext())
    )

    async with (
        asyncio.timeout(2),
        Client(server._mcp_server, mode="2026-07-28") as client,
        client.listen(tools_list_changed=True) as changes,
    ):
        assert changes.honored.tools_list_changed is True
        await bus.publish(ToolsListChanged())
        assert isinstance(await anext(changes), ToolsListChanged)
