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
from biff.server.state import CompanionSession, ServerState, create_state
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


class TestTargetedPushNotification:
    """A targeted (``user:tty``) message's wake poke marks the inbox gate too.

    The message rides the talk-notify subject (``_publish_talk_notification``),
    not a second inbox-notify publish — proving the talk-SUB callback's
    wake-poke classification (``TalkNotification.is_wake_poke``) actually
    marks the same gate a broadcast's inbox-notify poke marks, so the
    gated tick does not defer this detection to the backstop.
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

        # Well under the 30s nap_interval backstop the gate would otherwise
        # fall back to — this proves the talk-SUB wake-poke path detected
        # it, not the periodic safety net.
        desc = await _wait_for_read_messages_description(
            kai_client, "1 unread", timeout=6.0
        )
        assert "1 unread" in desc


class TestCompanionPushNotification:
    """A broadcast addressed to the companion's user pokes a subject only
    the companion binding subscribes to — proving ``poll_inbox`` opens a
    second, independent ``inbox_notify`` SUB when ``state.companion`` is
    set, not just one bound to ``state.config.user``.

    Builds both sides directly (rather than the shared ``kai_tracked`` /
    ``eric_tracked`` fixtures) so both share this file's own
    ``_TEST_REPO`` unambiguously — those shared fixtures live in
    ``conftest.py`` under a different repo constant, and a companion
    broadcast landing in the wrong repo's stream is exactly the kind of
    silent cross-repo mismatch this test would otherwise fail to catch.
    """

    async def test_companion_addressed_broadcast_wakes_and_refreshes(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        kai_state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server),
            tmp_path / "kai-companion",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
            companion=CompanionSession(
                user="jfreeman",
                display_name="Jim Freeman",
                kind="human",
                tty="bbbb0099",
            ),
        )
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server),
            tmp_path / "eric-companion",
            tty="eeee0098",
            hostname="test-host",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        async with (
            Client(FastMCPTransport(kai_mcp)) as kai_client,
            Client(FastMCPTransport(eric_mcp)),
        ):
            assert kai_state.companion is not None
            await asyncio.sleep(3.0)  # let the poller establish both inbox-notify SUBs

            await eric_state.relay.deliver(
                Message(
                    from_user="eric",
                    to_user=kai_state.companion.user,  # addressed to the companion
                    body="for the human",
                )
            )

            # The combined unread count (primary + companion,
            # _relay_pushes_inbox_notify's else-branch mirrors this sum for the
            # LocalRelay fallback) must reflect the companion's message well
            # inside push latency, not the 30s backstop.
            desc = await _wait_for_read_messages_description(
                kai_client, "1 unread", timeout=6.0
            )
            assert "1 unread" in desc


class TestCompanionTargetedPushNotification:
    """A *targeted* (``user:tty``) write to the companion's own session key
    rides the companion's talk-notify subject, not its inbox-notify one —
    proving ``poll_inbox`` opens a second talk-notify SUB bound to
    ``state.companion.session_key``, distinct from both this session's own
    talk SUB and either inbox-notify SUB. Before this SUB existed, nothing
    in this process subscribed to that subject at all, so a targeted write
    to the companion regressed to the backstop exactly as the untargeted
    companion gap did before its own fix.
    """

    async def test_targeted_write_to_companion_wakes_and_refreshes(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        kai_state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server),
            tmp_path / "kai-companion-targeted",
            tty=_KAI_TTY,
            hostname="test-host",
            pwd="/test",
            companion=CompanionSession(
                user="jfreeman",
                display_name="Jim Freeman",
                kind="human",
                tty="bbbb0098",
            ),
        )
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO, relay_url=nats_server),
            tmp_path / "eric-companion-targeted",
            tty="eeee0097",
            hostname="test-host",
            pwd="/test",
        )
        kai_mcp = create_server(kai_state)
        eric_mcp = create_server(eric_state)

        async with (
            Client(FastMCPTransport(kai_mcp)) as kai_client,
            Client(FastMCPTransport(eric_mcp)),
        ):
            assert kai_state.companion_session_key is not None
            await asyncio.sleep(3.0)  # let the poller establish the companion talk SUB

            await eric_state.relay.deliver(
                Message(
                    from_user="eric",
                    to_user=kai_state.companion_session_key,  # targeted — user:tty
                    body="direct ping for the human",
                ),
                sender_key=eric_state.session_key,
            )

            # Well under the 30s nap_interval backstop — proves the companion
            # talk-notify wake poke detected it, not the periodic safety net.
            desc = await _wait_for_read_messages_description(
                kai_client, "1 unread", timeout=6.0
            )
            assert "1 unread" in desc


class TestPollerAtDisabledInterval:
    """``poll_interval<=0`` still hosts the always-on SUBs and detects a poke.

    Proves the poller task always runs (``app.py``'s lifespan no longer
    skips creating it at interval<=0) and that push detection survives
    disabling the periodic cadence entirely — the scenario
    ``set_poll_interval``'s ``n`` response now claims works.
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
            Client(FastMCPTransport(kai_mcp)) as kai_client,
            Client(FastMCPTransport(eric_mcp)),
        ):
            await asyncio.sleep(1.0)  # let the poller establish its SUBs

            await eric_state.relay.deliver(
                Message(from_user="eric", to_user="kai", body="pushed despite n")
            )

            # No periodic tick exists to eventually catch this on its own —
            # if the wake_event/gate path is not doing the work, this
            # would hang until the timeout with no other path to succeed.
            desc = await _wait_for_read_messages_description(
                kai_client, "1 unread", timeout=6.0
            )
            assert "1 unread" in desc
