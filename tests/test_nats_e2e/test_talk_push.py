"""NATS E2E tests for talk push notifications.

Exercises the full callback chain:
  _on_talk_msg → refresh_talk → notify_tool_list_changed

Two MCP servers (kai and eric) backed by NatsRelay via FastMCPTransport.
An external NATS client publishes talk notifications to simulate the
CLI → MCP push path.  Tests verify that:

- The talk tool description updates with the incoming message
- ``tool_list_changed`` notifications fire via the suspenders path
- The display queue receives talk items for status bar rotation
- Rapid messages from the same sender coalesce (replace, not accumulate)
- Self-echo is rejected (messages from own session key are ignored)
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import nats as nats_lib
import pytest
from mcp.types import TextContent

from biff.server.state import ServerState
from biff.testing import NotificationTracker

if TYPE_CHECKING:
    from fastmcp import Client

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-nats-e2e"


async def _publish_talk_notification(
    nc: Any,
    repo: str,
    to_user: str,
    *,
    from_user: str,
    body: str,
    from_key: str,
) -> None:
    """Publish a talk notification on the core NATS subject."""
    subject = f"biff.{repo}.talk.notify.{to_user}"
    payload = json.dumps({"from": from_user, "body": body, "from_key": from_key})
    await nc.publish(subject, payload.encode())  # pyright: ignore[reportUnknownMemberType]
    await nc.flush()  # pyright: ignore[reportUnknownMemberType]


async def _wait_for_talk_description(
    client: Client[Any], pattern: str, *, timeout: float = 5.0
) -> str:
    """Poll ``list_tools()`` until the talk description contains *pattern*.

    Returns the matching description, or raises ``TimeoutError``.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "talk" and pattern in (tool.description or ""):
                return tool.description or ""
        await asyncio.sleep(0.2)
    msg = f"Talk description never contained {pattern!r} within {timeout}s"
    raise TimeoutError(msg)


async def _get_talk_description(client: Client[Any]) -> str:
    """Return the current talk tool description."""
    tools = await client.list_tools()
    for tool in tools:
        if tool.name == "talk":
            return tool.description or ""
    msg = "talk tool not found"
    raise AssertionError(msg)


class TestTalkPushNotification:
    """Test that external NATS talk notifications update MCP state."""

    async def _setup_talk_session(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """Register eric online and start kai's talk session with eric."""
        # eric must be online
        await eric_client.call_tool("plan", {"message": "available"})
        # kai must be online too (for session registration)
        await kai_client.call_tool("plan", {"message": "working"})
        # Start the talk session — this sets _talk_partner
        result = await kai_client.call_tool("talk", {"to": "@eric"})
        text_parts = [b.text for b in result.content if isinstance(b, TextContent)]
        assert any("Talk session started" in t for t in text_parts)

    async def test_notification_updates_talk_description(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """External notification changes the talk tool description."""
        kai_client, _kai_tracker, _kai_state = kai_tracked
        eric_client, _eric_tracker, _eric_state = eric_tracked

        await self._setup_talk_session(kai_client, eric_client)

        # Wait for the poller to establish the NATS subscription
        # (default 2s poll interval + buffer)
        await asyncio.sleep(3.0)

        # Publish a talk notification from eric to kai
        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_notification(
                nc,
                _TEST_REPO,
                "kai",
                from_user="eric",
                body="PR looks good",
                from_key=f"eric:{_eric_state.tty}",
            )
            # Wait for description to update
            desc = await _wait_for_talk_description(kai_client, "PR looks good")
            assert "[TALK]" in desc
            assert "PR looks good" in desc
        finally:
            await nc.close()

    async def test_notification_fires_tool_list_changed(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """External notification triggers tool_list_changed via suspenders."""
        kai_client, kai_tracker, _kai_state = kai_tracked
        eric_client, _eric_tracker, _eric_state = eric_tracked

        await self._setup_talk_session(kai_client, eric_client)
        await asyncio.sleep(3.0)

        before = kai_tracker.tool_list_changed_count

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_notification(
                nc,
                _TEST_REPO,
                "kai",
                from_user="eric",
                body="notification test",
                from_key=f"eric:{_eric_state.tty}",
            )
            # Wait for the notification to propagate
            await _wait_for_talk_description(kai_client, "notification test")
            assert kai_tracker.tool_list_changed_count > before
        finally:
            await nc.close()

    async def test_notification_adds_display_item(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """External notification adds a talk item to the display queue."""
        kai_client, _kai_tracker, kai_state = kai_tracked
        eric_client, _eric_tracker, _eric_state = eric_tracked

        await self._setup_talk_session(kai_client, eric_client)
        await asyncio.sleep(3.0)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_notification(
                nc,
                _TEST_REPO,
                "kai",
                from_user="eric",
                body="display queue test",
                from_key=f"eric:{_eric_state.tty}",
            )
            await _wait_for_talk_description(kai_client, "display queue test")

            items = kai_state.display_queue.snapshot()
            talk_items = [i for i in items if i.kind == "talk"]
            assert len(talk_items) == 1
            assert "display queue test" in talk_items[0].text
            assert talk_items[0].source_key == "talk:eric"
        finally:
            await nc.close()

    async def test_notification_coalesces_rapid_messages(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """Rapid messages from the same sender produce exactly 1 display item."""
        kai_client, _kai_tracker, kai_state = kai_tracked
        eric_client, _eric_tracker, _eric_state = eric_tracked

        await self._setup_talk_session(kai_client, eric_client)
        await asyncio.sleep(3.0)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            for i in range(3):
                await _publish_talk_notification(
                    nc,
                    _TEST_REPO,
                    "kai",
                    from_user="eric",
                    body=f"rapid message {i}",
                    from_key=f"eric:{_eric_state.tty}",
                )
            # Wait for the last message to land
            await _wait_for_talk_description(kai_client, "rapid message 2")

            items = kai_state.display_queue.snapshot()
            talk_items = [i for i in items if i.kind == "talk"]
            assert len(talk_items) == 1
            assert "rapid message 2" in talk_items[0].text
        finally:
            await nc.close()

    async def test_self_echo_rejected(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """Notifications from own session key are ignored (self-echo filter)."""
        kai_client, _kai_tracker, kai_state = kai_tracked
        eric_client, _eric_tracker, _eric_state = eric_tracked

        await self._setup_talk_session(kai_client, eric_client)
        await asyncio.sleep(3.0)

        # Get baseline description
        desc_before = await _get_talk_description(kai_client)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            # Publish with kai's own session key as from_key
            await _publish_talk_notification(
                nc,
                _TEST_REPO,
                "kai",
                from_user="eric",
                body="should be ignored",
                from_key=kai_state.session_key,
            )
            # Give time for the notification to (not) propagate
            await asyncio.sleep(1.0)

            desc_after = await _get_talk_description(kai_client)
            assert desc_before == desc_after
            assert "should be ignored" not in desc_after
        finally:
            await nc.close()
