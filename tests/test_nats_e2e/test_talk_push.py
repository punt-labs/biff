"""NATS E2E tests for talk push notifications (ephemeral model).

Exercises the full always-on chain:
  subscribe_talk → TalkState.receive → poller wake → refresh_talk →
  notify_tool_list_changed → model calls talk_read

Two MCP servers (kai and eric) backed by NatsRelay via FastMCPTransport.
An external NATS client publishes talk frames to simulate the CLI → MCP
push path.  Tests verify that an unsolicited frame is held in the server's
TalkState, the talk tool description updates to prompt ``talk_read``,
``tool_list_changed`` fires, ``talk_read`` returns the content, and
self-echo is rejected.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import nats as nats_lib
import pytest
from mcp.types import TextContent

from biff.nats_relay import NatsRelay
from biff.server.state import ServerState
from biff.testing import NotificationTracker

if TYPE_CHECKING:
    from fastmcp import Client

pytestmark = pytest.mark.nats


async def _publish_talk_frame(
    nc: Any,
    *,
    ntype: str = "message",
    from_user: str,
    body: str,
    from_key: str,
    to_key: str,
) -> None:
    """Publish a talk frame on the recipient's identity core subject."""
    subject = f"biff.talk.notify.{to_key}"
    payload = json.dumps(
        {
            "type": ntype,
            "from": from_user,
            "body": body,
            "from_key": from_key,
            "to_key": to_key,
        }
    )
    await nc.publish(subject, payload.encode())  # pyright: ignore[reportUnknownMemberType]
    await nc.flush()  # pyright: ignore[reportUnknownMemberType]


async def _wait_for_talk_description(
    client: Client[Any], pattern: str, *, timeout: float = 6.0
) -> str:
    """Poll ``list_tools()`` until the talk description contains *pattern*."""
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


async def _talk_read(client: Client[Any]) -> str:
    """Call talk_read and return its text output."""
    result = await client.call_tool("talk_read", {})
    return "\n".join(b.text for b in result.content if isinstance(b, TextContent))


class TestTalkPushNotification:
    """An external NATS talk frame updates held state and prompts talk_read."""

    async def _register(
        self, kai_client: Client[Any], eric_client: Client[Any]
    ) -> None:
        await eric_client.call_tool("plan", {"message": "available"})
        await kai_client.call_tool("plan", {"message": "working"})

    async def test_frame_updates_talk_description(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """An external message frame flips the description to prompt talk_read."""
        kai_client, _kt, kai_state = kai_tracked
        eric_client, _et, _es = eric_tracked
        await self._register(kai_client, eric_client)
        await asyncio.sleep(3.0)  # let the poller establish subscribe_talk

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_frame(
                nc,
                from_user="eric",
                body="PR looks good",
                from_key="eric:tty2",
                to_key=kai_state.session_key,
            )
            desc = await _wait_for_talk_description(kai_client, "[TALK]")
            assert "new message" in desc
            # The body is surfaced by talk_read, not the description.
            read = await _talk_read(kai_client)
            assert "PR looks good" in read
        finally:
            await nc.close()

    async def test_frame_fires_tool_list_changed(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """An external frame triggers tool_list_changed via the suspenders path."""
        kai_client, kai_tracker, kai_state = kai_tracked
        eric_client, eric_tracker, _es = eric_tracked
        await self._register(kai_client, eric_client)
        await asyncio.sleep(3.0)

        # The poller fires the list-changed push via the stored ServerSession.
        # Both in-process servers share that module-level reference (existing
        # design), so count across both trackers to prove a push fired.
        def _count() -> int:
            return (
                kai_tracker.tool_list_changed_count
                + eric_tracker.tool_list_changed_count
            )

        before = _count()
        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_frame(
                nc,
                from_user="eric",
                body="notification test",
                from_key="eric:tty2",
                to_key=kai_state.session_key,
            )
            await _wait_for_talk_description(kai_client, "[TALK]")
            # The suspenders notification is delivered asynchronously — poll for it.
            deadline = asyncio.get_event_loop().time() + 3.0
            while _count() <= before and asyncio.get_event_loop().time() < deadline:
                await kai_client.list_tools()
                await asyncio.sleep(0.2)
            assert _count() > before
        finally:
            await nc.close()

    async def test_read_returns_all_rapid_messages(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """Rapid messages are all held and surfaced by a single talk_read."""
        kai_client, _kt, kai_state = kai_tracked
        eric_client, _et, _es = eric_tracked
        await self._register(kai_client, eric_client)
        await asyncio.sleep(3.0)

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            for i in range(3):
                await _publish_talk_frame(
                    nc,
                    from_user="eric",
                    body=f"rapid message {i}",
                    from_key="eric:tty2",
                    to_key=kai_state.session_key,
                )
            await _wait_for_talk_description(kai_client, "[TALK]")
            read = await _talk_read(kai_client)
            assert "rapid message 0" in read
            assert "rapid message 2" in read
        finally:
            await nc.close()

    async def test_self_echo_rejected(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """Frames from our own session key are ignored (self-echo filter)."""
        kai_client, _kt, kai_state = kai_tracked
        eric_client, _et, _es = eric_tracked
        await self._register(kai_client, eric_client)
        await asyncio.sleep(3.0)

        desc_before = await _get_talk_description(kai_client)
        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            await _publish_talk_frame(
                nc,
                from_user="eric",
                body="should be ignored",
                from_key=kai_state.session_key,
                to_key=kai_state.session_key,
            )
            await asyncio.sleep(1.0)
            assert kai_state.talk.queued == 0
            desc_after = await _get_talk_description(kai_client)
            assert desc_before == desc_after
        finally:
            await nc.close()

    async def test_resubscribes_after_client_replacement(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """A forced client replacement re-establishes the orphaned talk SUB.

        Closing the poller's NATS client and redialing is the biff-3hp
        force-reconnect signature: the always-on talk SUB is orphaned on the
        closed client, and the fresh client carries none.  The poller must
        detect the generation bump and re-subscribe, or an unsolicited invite
        is silently lost (biff-9la) — the destructive biff-3hp x biff-9la
        interaction.  Since the old client is closed, a frame can only reach
        ``TalkState`` through a SUB re-established on the new client
        (``nats-relay.tex`` ``talkSubGen``).
        """
        kai_client, _kt, kai_state = kai_tracked
        eric_client, _et, _es = eric_tracked
        await self._register(kai_client, eric_client)
        await asyncio.sleep(3.0)  # let the poller establish subscribe_talk

        relay = kai_state.relay
        assert isinstance(relay, NatsRelay)
        bound_generation = relay.connection_generation

        # Force the client replacement: close the live client, then redial.
        # The next dial bumps the generation past the SUB's binding.
        nc1 = await relay.get_nc()
        await nc1.close()
        await relay.get_nc()  # dial the fresh client (nc#2)
        assert relay.connection_generation > bound_generation

        nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            # The poller re-subscribes on its next tick after the generation
            # bump; retry the publish until the fresh SUB catches a frame. A
            # frame published before the re-subscribe lands is dropped (core
            # NATS has no listener), so a single publish would race the tick.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 12.0
            desc = ""
            while loop.time() < deadline:
                await _publish_talk_frame(
                    nc,
                    from_user="eric",
                    body="after reconnect",
                    from_key="eric:tty2",
                    to_key=kai_state.session_key,
                )
                await asyncio.sleep(1.0)
                desc = await _get_talk_description(kai_client)
                if "[TALK]" in desc:
                    break
            assert "[TALK]" in desc
            assert "new message" in desc
            read = await _talk_read(kai_client)
            assert "after reconnect" in read
        finally:
            await nc.close()
