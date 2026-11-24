"""Compare push-driven broadcast detection with an explicit read.

Push trials use relay delivery, including its notification poke. Poke-less
trials publish directly to JetStream, so the receiver has nothing to wake
it: the server runs no timer (DES-087), and only an explicit read finds
the message.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message, UnreadSummary
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.tools._descriptions import _reset_session
from biff.testing import NotificationTracker
from biff.testing.caller import StampedClient
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastmcp import Client

    from biff.server.state import ServerState

pytestmark = pytest.mark.nats_local

_TEST_REPO = "_test-push-vs-poll-latency"
_KAI_TTY = "aaaa00f1"
_TRIALS = 3

# Longer than the retired 2s poll tick: a poke-less message that no timer
# finds in this window is not going to be found by one.
_NO_TIMER_WAIT = 3.0

# Bounds a single push trial's wait for the description to reflect the new
# unread count.  Generous so a slow CI runner doesn't turn a real detection
# into a false miss (the -1.0 sentinel below is what a genuine miss reports).
_TRIAL_TIMEOUT = 6.0


async def _publish_broadcast_without_poke(
    sender: NatsRelay, *, to_user: str, from_user: str, body: str
) -> None:
    """Publish a broadcast message straight to JetStream, bypassing the poke.

    The message lands in the durable inbox without a wake poke, so the poller
    must discover it through the ``_InboxPokeGate`` backstop.

    Uses ``sender``'s own JetStream context and subject-naming method
    (private, white-box access — mirrors ``test_inbox_push.py``'s
    ``TestPokeSubjectDoesNotCollideWithInboxStream`` and
    ``test_push_demo.py``'s docker-tier counterpart) instead of a second
    bare connection and a hand-copied subject string, so this helper
    can never silently drift from ``deliver()``'s own subject shape.
    """
    js, _ = await sender._ensure_connected()  # white-box bypass, see docstring
    msg = Message(from_user=from_user, to_user=to_user, body=body)
    subject = sender._user_subject(to_user)  # white-box bypass, see docstring
    await js.publish(  # pyright: ignore[reportUnknownMemberType]
        subject,
        msg.model_dump_json().encode(),
        headers={"Nats-Msg-Id": str(uuid.uuid4())},
    )


async def _wait_for_unread_description(
    client: Client[Any], *, timeout: float
) -> float | None:
    """Poll ``list_tools()`` until ``read_messages`` shows "1 unread".

    Returns the ``time.monotonic()`` reading at the moment the mutated
    description was observed, or ``None`` on timeout (the caller
    converts this to the ``-1.0`` miss sentinel).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "read_messages" and "1 unread" in (tool.description or ""):
                return time.monotonic()
        await asyncio.sleep(0.1)
    return None


async def _drain_inbox(client: Client[Any]) -> None:
    """Reset ``kai``'s inbox to zero unread between trials."""
    await client.call_tool("read_messages", {})


class _LatencyTrials:
    """Runs one arm (push or poll) of the A/B comparison and reports results.

    Not a domain entity biff ships — a throwaway measurement harness
    scoped to this one test module, so it lives here rather than in
    ``src/biff/testing``.
    """

    __slots__ = ("_client", "_kai_state", "_tracker")

    def __init__(
        self,
        client: Client[Any],
        tracker: NotificationTracker,
        kai_state: ServerState,
    ) -> None:
        self._client = client
        self._tracker = tracker
        self._kai_state = kai_state

    async def run_push(self, sender: NatsRelay) -> list[float]:
        """Measure push-path (real ``deliver()``) latency, ``_TRIALS`` times."""
        latencies: list[float] = []
        for i in range(_TRIALS):
            t_send = time.monotonic()
            await sender.deliver(
                Message(
                    from_user="eric",
                    to_user=self._session_user(),
                    body=f"push trial {i}",
                )
            )
            t_recv = await _wait_for_unread_description(
                self._client, timeout=_TRIAL_TIMEOUT
            )
            latencies.append(t_recv - t_send if t_recv is not None else -1.0)
            await _drain_inbox(self._client)
        return latencies

    async def run_unpoked(self, sender: NatsRelay) -> list[bool]:
        """For each trial, whether the poke-less message stayed undetected.

        A trial passes (``True``) when the description still shows no unread
        mail after ``_NO_TIMER_WAIT`` and an explicit ``read_messages`` then
        returns the message.
        """
        outcomes: list[bool] = []
        for i in range(_TRIALS):
            await _publish_broadcast_without_poke(
                sender,
                to_user=self._session_user(),
                from_user="eric",
                body=f"unpoked trial {i}",
            )
            await asyncio.sleep(_NO_TIMER_WAIT)
            undetected = (
                await _wait_for_unread_description(self._client, timeout=0.2) is None
            )
            result = await self._client.call_tool("read_messages", {})
            outcomes.append(undetected and f"unpoked trial {i}" in str(result))
        return outcomes

    def _session_user(self) -> str:
        return self._kai_state.config.user


def _print_comparison(push: list[float], unpoked: list[bool]) -> None:
    """Print the push latencies and the poke-less outcomes."""
    push_valid = [t for t in push if t >= 0]
    push_missed = sum(1 for t in push if t < 0)

    print(f"\n{'Path':<8} {'Avg (ms)':>10} {'Max (ms)':>10} {'Missed':>8}")
    print("-" * 40)
    if push_valid:
        print(
            f"{'push':<8} "
            f"{sum(push_valid) * 1000 / len(push_valid):>10.1f} "
            f"{max(push_valid) * 1000:>10.1f} "
            f"{push_missed:>8}"
        )
    held = sum(unpoked)
    print(f"\nno poke: {held}/{len(unpoked)} waited for an explicit read")


@pytest.fixture
async def kai_latency(
    nats_server: str, tmp_path: Path
) -> AsyncIterator[tuple[Client[Any], NotificationTracker, ServerState]]:
    """A ``kai`` MCP server for both arms."""
    _reset_session()
    tracker = NotificationTracker()
    config = BiffConfig(
        user="kai",
        repo_name=_TEST_REPO,
        relay_url=nats_server,
    )
    state = create_state(
        config, tmp_path / "kai", tty=_KAI_TTY, hostname="test-host", pwd="/test"
    )
    mcp = create_server(state)
    async with StampedClient(FastMCPTransport(mcp), message_handler=tracker) as client:
        await client.call_tool("who", {})
        yield client, tracker, state
    _reset_session()


@pytest.fixture
async def sender_relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """A bare ``NatsRelay`` for the sender side — no MCP server needed."""
    relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
    try:
        yield relay
    finally:
        await relay.close()


class TestPushVsPollLatency:
    """Push detects a broadcast; with no poke, no timer does."""

    async def test_push_beats_poll(
        self,
        kai_latency: tuple[Client[Any], NotificationTracker, ServerState],
        sender_relay: NatsRelay,
    ) -> None:
        kai_client, _tracker, kai_state = kai_latency
        await asyncio.sleep(1.0)  # let the poller establish subscribe_inbox_notify

        trials = _LatencyTrials(kai_client, _tracker, kai_state)
        push_latencies = await trials.run_push(sender_relay)
        unpoked = await trials.run_unpoked(sender_relay)

        _print_comparison(push_latencies, unpoked)

        push_missed = sum(1 for t in push_latencies if t < 0)
        assert not push_missed, f"push missed {push_missed}/{_TRIALS} detections"
        push_avg = sum(push_latencies) / len(push_latencies)

        # Generous, CI-stable margin: push resolves in under 2s regardless
        # of runner slowness.
        assert push_avg < 2.0, f"push averaged {push_avg * 1000:.1f}ms, expected < 2s"
        assert all(unpoked), f"a timer found a poke-less message: {unpoked}"


class TestLoadReduction:
    """With no arrivals, the server asks the relay for unread counts only at start."""

    async def test_idle_unread_summary_calls_stop_after_startup(
        self,
        nats_server: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Idle time issues no ``stream_info`` round trip at all (DES-087)."""
        _reset_session()
        config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        state = create_state(
            config, tmp_path / "kai", tty=_KAI_TTY, hostname="test-host", pwd="/test"
        )

        call_times: list[float] = []
        original = state.relay.get_unread_summary

        async def _counting_get_unread_summary(session_key: str) -> UnreadSummary:
            call_times.append(time.monotonic())
            return await original(session_key)

        monkeypatch.setattr(
            state.relay, "get_unread_summary", _counting_get_unread_summary
        )

        mcp = create_server(state)
        async with StampedClient(FastMCPTransport(mcp)) as client:
            await client.call_tool("who", {})
            await asyncio.sleep(2.0)  # startup settles
            settled = len(call_times)
            await asyncio.sleep(_NO_TIMER_WAIT * 2)
            idle = len(call_times) - settled
        _reset_session()

        print(f"\nunread summary calls: {settled} at startup, {idle} while idle")
        assert settled, "the server never checked unread counts at startup"
        assert idle == 0, f"{idle} unread summary calls while idle: a timer is running"
