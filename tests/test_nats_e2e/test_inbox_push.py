"""NATS E2E tests for broadcast-message push notifications (biff-5ex, DES-062).

Exercises the full always-on chain for a broadcast (bare-user) message:
  deliver() -> inbox-notify poke -> subscribe_inbox_notify callback ->
  poller wake -> refresh_read_messages -> notify_tool_list_changed

Two MCP servers (kai and eric) backed by NatsRelay via FastMCPTransport.
Mirrors ``test_talk_push.py``'s structure for the analogous always-on SUB.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import nats as nats_lib
import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools._descriptions import nap_interval_for
from biff.testing import NotificationTracker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

pytestmark = pytest.mark.nats_local

_TEST_REPO = "_test-nats-e2e-inbox-push"
_KAI_TTY = "aaaa0011"

# A short poll_interval keeps the backstop (nap_interval_for(poll_interval),
# DES-062) fast enough to wait out in a test — 0.3s active cadence yields a
# 4.5s backstop, versus the 30s a production default would require.
_FAST_POLL_INTERVAL = 0.3


async def _wait_for_read_messages_description(
    client: Client[Any], pattern: str, *, timeout: float = 6.0
) -> str:
    """Poll ``list_tools()`` until the read_messages description contains *pattern*."""
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "read_messages" and pattern in (tool.description or ""):
                return tool.description or ""
        await asyncio.sleep(0.2)
    msg = f"read_messages description never contained {pattern!r} within {timeout}s"
    raise TimeoutError(msg)


async def _publish_broadcast_without_poke(
    nats_server: str, *, repo: str, to_user: str, from_user: str, body: str
) -> None:
    """Publish a broadcast message straight to JetStream, bypassing the poke.

    Simulates a pre-biff-5ex sender (or any client that only knows the
    durable-inbox subject): the message lands in the durable inbox exactly
    as ``deliver()`` would land it, but no wake poke is published — the
    only way the recipient's poller can find it is the nap_interval
    backstop (DES-062 §2/§3), not the push path.
    """
    nc = await nats_lib.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
    try:
        js = nc.jetstream()  # pyright: ignore[reportUnknownMemberType]
        msg = Message(from_user=from_user, to_user=to_user, body=body)
        subject = f"biff.{repo}.inbox.{to_user}"
        await js.publish(  # pyright: ignore[reportUnknownMemberType]
            subject,
            msg.model_dump_json().encode(),
            headers={"Nats-Msg-Id": str(uuid.uuid4())},
        )
    finally:
        await nc.close()  # pyright: ignore[reportUnknownMemberType]


class TestBroadcastPushNotification:
    """A broadcast deliver() pokes the recipient's poller into an immediate refresh."""

    async def test_broadcast_deliver_wakes_and_refreshes_description(
        self,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """deliver()'s broadcast branch pokes kai's poller into refreshing."""
        kai_client, kai_tracker, kai_state = kai_tracked
        _ec, eric_tracker, eric_state = eric_tracked
        await asyncio.sleep(3.0)  # let the poller establish subscribe_inbox_notify

        # The suspenders send uses the module-level captured ``_session`` —
        # shared by both in-process servers in this test (existing design,
        # see test_talk_push.py) — so count across both trackers to prove a
        # push fired, the same way test_talk_push.py does.
        def _count() -> int:
            return (
                kai_tracker.tool_list_changed_count
                + eric_tracker.tool_list_changed_count
            )

        before = _count()
        await eric_state.relay.deliver(
            Message(
                from_user="eric", to_user=kai_state.config.user, body="standup notes"
            )
        )

        # The poke wakes the poller well inside a couple of active-tick
        # intervals — far under the 30s nap_interval backstop this proves
        # is not what detected it.
        desc = await _wait_for_read_messages_description(
            kai_client, "1 unread", timeout=6.0
        )
        assert "1 unread" in desc

        deadline = asyncio.get_event_loop().time() + 3.0
        while _count() <= before and asyncio.get_event_loop().time() < deadline:
            await kai_client.list_tools()
            await asyncio.sleep(0.2)
        assert _count() > before

    async def test_resubscribes_after_client_replacement(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        """A forced client replacement re-establishes the orphaned inbox-notify SUB.

        Mirrors ``test_talk_push.py``'s
        ``test_resubscribes_after_client_replacement`` for the second
        always-on SUB the ``SubKind``-indexed family (``nats-relay.tex``
        ``subGen``) adds: closing the poller's NATS client and redialing is
        the proactive force-reconnect signature, orphaning the inbox-notify
        SUB on the closed client exactly as it would the talk SUB.
        """
        kai_client, _kt, kai_state = kai_tracked
        _ec, _et, eric_state = eric_tracked
        await asyncio.sleep(3.0)  # let the poller establish subscribe_inbox_notify

        relay = kai_state.relay
        assert isinstance(relay, NatsRelay)
        bound_generation = relay.connection_generation

        nc1 = await relay.get_nc()
        await nc1.close()
        await relay.get_nc()  # dial the fresh client
        assert relay.connection_generation > bound_generation

        # The poller re-subscribes on its next tick after the generation
        # bump; retry the broadcast until the fresh SUB catches the poke.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 12.0
        desc = ""
        while loop.time() < deadline:
            await eric_state.relay.deliver(
                Message(
                    from_user="eric",
                    to_user=kai_state.config.user,
                    body="after reconnect",
                )
            )
            await asyncio.sleep(1.0)
            tools = await kai_client.list_tools()
            for tool in tools:
                if tool.name == "read_messages":
                    desc = tool.description or ""
            if "unread" in desc:
                break
        assert "unread" in desc


@pytest.fixture
async def kai_fast_backstop(
    nats_server: str, tmp_path: Path
) -> AsyncIterator[tuple[Client[Any], ServerState]]:
    """A kai MCP server whose ``poll_interval`` yields a short, waitable backstop.

    The shared ``kai_tracked`` fixture defaults to the production 2s/30s
    active/backstop cadence — too slow to wait out in a test.
    ``nap_interval_for`` ties the backstop cadence to ``poll_interval``
    (DES-062), so a fast ``poll_interval`` here yields a fast backstop too.
    """
    config = BiffConfig(
        user="kai",
        repo_name=_TEST_REPO,
        relay_url=nats_server,
        poll_interval=_FAST_POLL_INTERVAL,
    )
    state = create_state(
        config, tmp_path / "kai", tty=_KAI_TTY, hostname="test-host", pwd="/test"
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client, state


class TestUnpushedBroadcastBackstop:
    """A JetStream-only broadcast (no poke) is still caught by the backstop."""

    async def test_unpushed_broadcast_detected_via_backstop(
        self,
        nats_server: str,
        kai_fast_backstop: tuple[Client[Any], ServerState],
    ) -> None:
        kai_client, kai_state = kai_fast_backstop
        await asyncio.sleep(1.0)  # let the poller establish its initial ticks

        backstop = nap_interval_for(_FAST_POLL_INTERVAL)
        await _publish_broadcast_without_poke(
            nats_server,
            repo=_TEST_REPO,
            to_user=kai_state.config.user,
            from_user="eric",
            body="no poke here",
        )

        desc = await _wait_for_read_messages_description(
            kai_client, "1 unread", timeout=backstop + 6.0
        )
        assert "1 unread" in desc
