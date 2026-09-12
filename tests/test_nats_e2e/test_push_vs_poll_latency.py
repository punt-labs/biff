"""Comparative demonstration: push vs. poll for broadcast message detection.

Proves DES-062's measurable claim (biff-5ex) with an A/B lever on the
SENDER side only — the receiving MCP server (``kai``) runs the same,
honest code in both arms:

* **poll** trials publish a broadcast message straight to JetStream via
  the relay's own handle, bypassing :meth:`NatsRelay.deliver`'s wake
  poke entirely — exactly what the pre-biff-5ex ``deliver()`` did.  The
  receiver's poller then has no poke to react to, so ``kai``'s unread
  count only advances on the poke-gated backstop tick
  (``_InboxPokeGate``, ``_descriptions.py``).
* **push** trials go through the real :meth:`NatsRelay.deliver`, which
  publishes to JetStream *and* the inbox-notify wake poke on top.

Both arms are measured end to end: JetStream publish -> (poke, poll
arm has none) -> poller tick -> ``refresh_read_messages`` mutates the
``read_messages`` tool description -> ``notifications/tools/list_changed``
reaches this test's ``NotificationTracker``.

Mirrors ``test_notification_latency.py``'s pattern: ``_TRIALS``, the
``-1.0`` timeout sentinel, and a printed side-by-side comparison table.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message, UnreadSummary
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools._descriptions import _reset_session, nap_interval_for
from biff.testing import NotificationTracker

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.server.state import ServerState

pytestmark = pytest.mark.nats_local

_TEST_REPO = "_test-push-vs-poll-latency"
_KAI_TTY = "aaaa00f1"
_TRIALS = 3

# Fast enough to wait out in a test (0.3s active tick, matching
# test_inbox_push.py's kai_fast_backstop) — nap_interval_for's 15x ratio
# scales this to a 4.5s backstop, versus a 30s production default.
_POLL_INTERVAL = 0.3
_BACKSTOP = nap_interval_for(_POLL_INTERVAL)

# Bounds a single trial's wait for the description to reflect the new
# unread count.  Generous relative to _BACKSTOP so a slow CI runner
# doesn't turn a real detection into a false miss (the -1.0 sentinel
# below, not this bound, is what a genuine miss reports as).
_TRIAL_TIMEOUT = _BACKSTOP * 2.0

# Synchronization pause before each poll trial: deliberately longer
# than one full _BACKSTOP, so a message-less recompute is guaranteed
# to happen at least once during the sleep, resetting the gate's
# clock — without this, a trial's phase relative to the backstop's
# periodic schedule is wherever the previous trial's wall-clock
# overhead happened to leave it, and the observed latency swings
# unpredictably across [0, _BACKSTOP) run to run.
#
# The pause *oversleeps* past that reset by roughly 2 * _POLL_INTERVAL
# plus jitter, so the trial's message is actually published partway
# into the *next* backstop window, not right at its start — the next
# recompute is then less than a full _BACKSTOP away. Observed latency
# is consistently around two-thirds of _BACKSTOP (~2.9-3.0s of 4.5s
# across three separate runs), not the full interval. That's still
# what the demonstration needs: deterministic and reproducible run to
# run, clearly separated from push's sub-second latency, and
# comfortably above the assertion's half-backstop floor.
_SYNC_PAUSE = _BACKSTOP + 2 * _POLL_INTERVAL


async def _publish_broadcast_without_poke(
    sender: NatsRelay, *, to_user: str, from_user: str, body: str
) -> None:
    """Publish a broadcast message straight to JetStream, bypassing the poke.

    This is exactly what ``NatsRelay.deliver()``'s broadcast branch did
    before biff-5ex: the message lands in the durable inbox, but no wake
    poke is published, so the only way a poller can find it is the
    ``_InboxPokeGate`` backstop.

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

    async def run_poll(self, sender: NatsRelay) -> list[float]:
        """Measure poll-path (backstop-only) latency, ``_TRIALS`` times."""
        latencies: list[float] = []
        for i in range(_TRIALS):
            # Force a message-less backstop recompute before this trial's
            # publish, so the measurement is deterministic run to run —
            # see _SYNC_PAUSE's comment above for the traced timing.
            await asyncio.sleep(_SYNC_PAUSE)
            t_send = time.monotonic()
            await _publish_broadcast_without_poke(
                sender,
                to_user=self._session_user(),
                from_user="eric",
                body=f"poll trial {i}",
            )
            t_recv = await _wait_for_unread_description(
                self._client, timeout=_TRIAL_TIMEOUT
            )
            latencies.append(t_recv - t_send if t_recv is not None else -1.0)
            await _drain_inbox(self._client)
        return latencies

    def _session_user(self) -> str:
        return self._kai_state.config.user


def _print_comparison(push: list[float], poll: list[float]) -> None:
    """Print the side-by-side table, mirroring test_notification_latency.py."""
    push_valid = [t for t in push if t >= 0]
    poll_valid = [t for t in poll if t >= 0]
    push_missed = sum(1 for t in push if t < 0)
    poll_missed = sum(1 for t in poll if t < 0)

    print(f"\n{'Path':<8} {'Avg (ms)':>10} {'Max (ms)':>10} {'Missed':>8}")
    print("-" * 40)
    if push_valid:
        print(
            f"{'push':<8} "
            f"{sum(push_valid) * 1000 / len(push_valid):>10.1f} "
            f"{max(push_valid) * 1000:>10.1f} "
            f"{push_missed:>8}"
        )
    if poll_valid:
        print(
            f"{'poll':<8} "
            f"{sum(poll_valid) * 1000 / len(poll_valid):>10.1f} "
            f"{max(poll_valid) * 1000:>10.1f} "
            f"{poll_missed:>8}"
        )
    print(f"\nbackstop (nap_interval): {_BACKSTOP * 1000:.1f}ms")


@pytest.fixture
async def kai_latency(
    nats_server: str, tmp_path: Path
) -> AsyncIterator[tuple[Client[Any], NotificationTracker, ServerState]]:
    """A ``kai`` MCP server with a fast poll_interval, so both arms are waitable."""
    _reset_session()
    tracker = NotificationTracker()
    config = BiffConfig(
        user="kai",
        repo_name=_TEST_REPO,
        relay_url=nats_server,
        poll_interval=_POLL_INTERVAL,
    )
    state = create_state(
        config, tmp_path / "kai", tty=_KAI_TTY, hostname="test-host", pwd="/test"
    )
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp), message_handler=tracker) as client:
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
    """The comparative demonstration: push beats poll for broadcast detection."""

    async def test_push_beats_poll(
        self,
        kai_latency: tuple[Client[Any], NotificationTracker, ServerState],
        sender_relay: NatsRelay,
    ) -> None:
        kai_client, _tracker, kai_state = kai_latency
        await asyncio.sleep(1.0)  # let the poller establish subscribe_inbox_notify

        trials = _LatencyTrials(kai_client, _tracker, kai_state)
        push_latencies = await trials.run_push(sender_relay)
        poll_latencies = await trials.run_poll(sender_relay)

        _print_comparison(push_latencies, poll_latencies)

        push_missed = sum(1 for t in push_latencies if t < 0)
        assert not push_missed, f"push missed {push_missed}/{_TRIALS} detections"
        poll_missed = sum(1 for t in poll_latencies if t < 0)
        assert not poll_missed, f"poll missed {poll_missed}/{_TRIALS} detections"

        push_avg = sum(push_latencies) / len(push_latencies)
        poll_avg = sum(poll_latencies) / len(poll_latencies)

        # Generous, CI-stable margins (docs/design-nats-push.md): push
        # resolves in under 2s regardless of runner slowness; poll is
        # bound to at least half the backstop cadence because nothing
        # wakes it early.
        assert push_avg < 2.0, f"push averaged {push_avg * 1000:.1f}ms, expected < 2s"
        assert poll_avg >= _BACKSTOP * 0.5, (
            f"poll averaged {poll_avg * 1000:.1f}ms, "
            f"expected >= {_BACKSTOP * 500:.1f}ms (half the backstop)"
        )


class TestLoadReduction:
    """Steady-state unread ``stream_info`` load drops to backstop-only cadence."""

    async def test_idle_unread_summary_calls_bounded_by_backstop(
        self,
        nats_server: str,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """With no arrivals, ``get_unread_summary`` is called by the backstop only.

        Before biff-5ex, ``_active_tick`` called ``get_unread_summary``
        unconditionally on every active tick (one ``stream_info``
        round-trip per ``poll_interval``). The poke-gated backstop
        (``_InboxPokeGate``) replaces that with a call only on the
        initial tick and every ``nap_interval`` thereafter — this test
        counts the real calls made to the relay's own method (not log
        parsing) and checks that count against the backstop-only
        expectation, not the old per-tick one.
        """
        _reset_session()
        config = BiffConfig(
            user="kai",
            repo_name=_TEST_REPO,
            relay_url=nats_server,
            poll_interval=_POLL_INTERVAL,
        )
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
        idle_seconds = _BACKSTOP * 2.2
        async with Client(FastMCPTransport(mcp)):
            await asyncio.sleep(idle_seconds)
        _reset_session()

        old_style_calls = idle_seconds / _POLL_INTERVAL
        # Backstop-only: one per elapsed backstop interval, plus +3 —
        # 1 for app.py's lifespan, which calls refresh_read_messages
        # once before the poller's own first tick even runs, and 2 for
        # scheduling slop. Observed was exactly 4 across three separate
        # runs (int(9.9/4.5)=2 backstop recomputes + 1 lifespan + 1 of
        # the slop already used), so a bare +2 left zero margin above
        # the observed count — a latent flake waiting for a slow
        # runner. +3 leaves one full unit of headroom. Nowhere near the
        # old per-tick rate either way.
        expected_ceiling = int(idle_seconds / _BACKSTOP) + 3

        print(
            f"\nunread summary calls over {idle_seconds:.1f}s idle: "
            f"{len(call_times)} (backstop-only) "
            f"vs {old_style_calls:.0f} (old per-tick rate)"
        )
        assert len(call_times) <= expected_ceiling, (
            f"{len(call_times)} calls exceeds backstop-only ceiling "
            f"{expected_ceiling} — recompute is running on every tick again"
        )
        assert len(call_times) < old_style_calls / 2
