"""Exercise broadcast-message push notifications against real NATS.

Verify the always-on subscription chain wakes a separate MCP server and
refreshes its unread description.  The server runs no periodic poll
(DES-087), so a push is the only thing that wakes it.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Any

import nats as nats_lib
import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import ServerState
from biff.testing import NotificationTracker
from biff.testing.caller import StampedClient
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path

    from fastmcp import Client

pytestmark = pytest.mark.nats_local

_TEST_REPO = "_test-nats-e2e-inbox-push"
_KAI_TTY = "aaaa0011"


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
    """Publish directly to the durable inbox without a notification poke.

    Use this to isolate push-driven detection from an explicit read.
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

        # The poke wakes the poller at once; no timer exists to detect it.
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

        # The poller re-subscribes on its next liveness check after the
        # generation bump; retry the broadcast until the fresh SUB catches the poke.
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


class TestPokeSubjectDoesNotCollideWithInboxStream:
    """The poke subject must never match the durable inbox stream's filter.

    Round-1 evaluation (djb) found the original subject shape,
    ``{stream_prefix}.{repo}.inbox.notify.{user}``, matched the inbox
    stream's wildcard filter ``{stream_prefix}.*.inbox.>`` (``_provision``,
    ``nats_relay.py``): JetStream silently captured every poke into the
    shared WORK_QUEUE stream, with no consumer ever reading it and no
    ``max_age``/``max_msgs`` bound reclaiming it — proven live with a bare
    ``nc.publish()`` moving ``stream_info().state.messages`` 0 -> 1. This
    test makes that probe permanent against the fixed subject shape
    (``{stream_prefix}.{repo}.notify.{user}``, ``inbox`` replaced by
    ``notify``): the *unfiltered* message count on the inbox stream must be
    unchanged by a poke publish.
    """

    async def test_poke_publish_does_not_land_in_the_inbox_stream(
        self,
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        _ec, _et, eric_state = eric_tracked
        relay = eric_state.relay
        assert isinstance(relay, NatsRelay)
        js, _ = await relay._ensure_connected()

        before = await js.stream_info(relay._stream_name)
        before_count = before.state.messages

        await relay._publish_inbox_notification(relay._repo_name, "kai")
        # A captured message needs a moment to land in the stream's state —
        # stream_info() checked immediately after publish() returns can still
        # report the pre-publish count even when the subject collides (the
        # collision was reproduced directly against a live server: checking
        # too early made the bug invisible here too). Poll briefly rather
        # than assume a fixed settle time.
        deadline = asyncio.get_event_loop().time() + 3.0
        after_count = before_count
        while asyncio.get_event_loop().time() < deadline:
            after = await js.stream_info(relay._stream_name)
            after_count = after.state.messages
            if after_count != before_count:
                break
            await asyncio.sleep(0.2)

        assert after_count == before_count


class TestUnpushedBroadcast:
    """A JetStream-only broadcast (no poke) is found by an explicit read.

    There is no timer backstop (DES-087): nothing but a push wakes the
    poller, so the description stays unchanged until the user reads.
    """

    async def test_unpushed_broadcast_waits_for_an_explicit_read(
        self,
        nats_server: str,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        kai_client, _kt, kai_state = kai_tracked
        await asyncio.sleep(1.0)  # let the poller establish its initial pass

        await _publish_broadcast_without_poke(
            nats_server,
            repo=kai_state.config.repo_name,
            to_user=kai_state.config.user,
            from_user="eric",
            body="no poke here",
        )

        await asyncio.sleep(3.0)  # past the retired 2s tick
        tools = await kai_client.list_tools()
        desc = next(t.description or "" for t in tools if t.name == "read_messages")
        assert "unread" not in desc

        result = await kai_client.call_tool("read_messages", {})
        assert "no poke here" in str(result)


class TestTargetedPushNotification:
    """A targeted (``user:tty``) message's wake poke marks the inbox gate too.

    The message rides the talk-notify subject (``_publish_talk_notification``),
    not a second inbox-notify publish — proving the talk-SUB callback's
    wake-poke classification (``TalkNotification.is_wake_poke``) actually
    marks the same gate a broadcast's inbox-notify poke marks, so the
    gated tick does not wait for another arrival.
    """

    async def test_targeted_write_wakes_and_refreshes_description(
        self,
        kai_tracked: tuple[Client[Any], NotificationTracker, ServerState],
        eric_tracked: tuple[Client[Any], NotificationTracker, ServerState],
    ) -> None:
        kai_client, _kt, kai_state = kai_tracked
        _ec, _et, eric_state = eric_tracked
        await asyncio.sleep(3.0)  # let the poller establish subscribe_talk

        await eric_state.relay.deliver(
            Message(
                from_user="eric",
                to_user=kai_state.session_key,  # targeted — user:tty
                body="direct ping",
            ),
            sender_key=eric_state.session_key,
        )

        # This proves the talk-SUB wake-poke path detected it; no timer
        # exists to do so.
        desc = await _wait_for_read_messages_description(
            kai_client, "1 unread", timeout=6.0
        )
        assert "1 unread" in desc


class TestPollerAtDisabledInterval:
    """Keep push detection running when the user's polling is off.

    The always-on subscriptions are hosted whatever ``poll_interval`` is.
    """

    async def test_disabled_interval_still_detects_a_broadcast_poke(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        kai_state = create_state(
            BiffConfig(
                user="kai",
                repo_name=_TEST_REPO,
                relay_url=nats_server,
                poll_interval=0,
            ),
            tmp_path / "kai-disabled",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
        )
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server),
            tmp_path / "eric-disabled",
            tty="eeee0099",
            hostname="test-host",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        async with (
            StampedClient(FastMCPTransport(kai_mcp)) as kai_client,
            StampedClient(FastMCPTransport(eric_mcp)) as eric_client,
        ):
            await kai_client.call_tool("who", {})
            await eric_client.call_tool("who", {})
            await asyncio.sleep(1.0)  # let the poller establish its SUBs

            await eric_state.relay.deliver(
                Message(from_user="eric", to_user="kai", body="pushed despite n")
            )

            # No periodic tick exists to eventually catch this on its own —
            # if the wake/gate path is not doing the work, this would hang
            # until the timeout with no other path to succeed.
            desc = await _wait_for_read_messages_description(
                kai_client, "1 unread", timeout=6.0
            )
            assert "1 unread" in desc
