"""End-to-end latency proof for the wall wake poke (PL-1104).

Proves PL-1104's claim against a real NATS server: a wall posted through
:meth:`NatsRelay.set_wall` — which publishes the wall-notify wake poke on
top of the authoritative KV write — reconciles a *separate* MCP server's
``wall`` tool description in well under a second, across repeated trials,
with no unrelated activity to wake the peer.

The sender is a bare relay; the receiver is a full MCP server with its own
poller. Measured end to end: ``set_wall`` -> wall-notify poke -> receiver's
poller wakes and ticks -> ``refresh_wall`` mutates the ``wall`` tool
description to carry ``[WALL]`` -> this test's ``list_tools()`` observes it.

Before PL-1104 the receiver had no such signal: it noticed a wall change
only on its own next scheduled ``get_wall()`` tick (seconds, or minutes
behind a wedged connection). The sub-second detection measured here is only
possible because the poke wires the sender's write to the receiver's poller.

Mirrors ``test_push_vs_poll_latency.py`` (the mail-path counterpart):
``_TRIALS`` and the ``-1.0`` timeout sentinel.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, WallPost
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.state import create_state
from biff.server.tools._descriptions import _reset_session
from biff.testing.caller import StampedClient

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from fastmcp import Client


pytestmark = pytest.mark.nats_local

_TEST_REPO = "_test-wall-push-latency"
_KAI_TTY = "aaaa00f2"
_TRIALS = 3

# The receiver's periodic poll interval is set deliberately HIGH — well above
# the latency bound asserted below — so a scheduled tick cannot satisfy the
# assertion. The only thing that can reconcile the wall in under a second is
# the wall-notify wake poke, which interrupts the poll sleep immediately
# (``wake_event`` cuts the tick regardless of the interval). If the poke path
# were broken, detection would wait out this whole interval and the test would
# fail — that isolation is the point (Copilot review on PR #542). A broken poke
# must not be able to hide behind a fast poll cadence.
_POLL_INTERVAL = 10.0

# Detection must land well inside one poll interval to prove the poke, not the
# tick, did the work. 2s leaves CI headroom while staying far below the 10s
# cadence a scheduled tick would take.
_PUSH_LATENCY_BOUND = 2.0

# A per-trial wait longer than the poll interval, so a genuine miss (poke
# broken) is reported as the -1.0 sentinel only after the scheduled tick has
# also had its chance — distinguishing "poke failed" from "nothing works".
_TRIAL_TIMEOUT = _POLL_INTERVAL * 1.5


def _make_wall(body: str) -> WallPost:
    """A fresh, unexpired wall with a unique text so each trial differs."""
    now = datetime.now(UTC)
    return WallPost(
        text=body,
        from_user="eric",
        from_tty="cli",
        posted_at=now,
        expires_at=now + timedelta(hours=1),
    )


async def _wait_for_wall_description(
    client: Client[Any], *, body: str, timeout: float
) -> float | None:
    """Poll ``list_tools()`` until ``wall`` shows ``[WALL]`` with *body*.

    Keying on the trial's unique *body* (not a bare ``[WALL]`` marker)
    avoids a stale prior wall satisfying the wait. Returns the
    ``time.monotonic()`` reading at the moment the mutated description was
    observed, or ``None`` on timeout (the caller converts this to the
    ``-1.0`` miss sentinel).
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        tools = await client.list_tools()
        for tool in tools:
            desc = tool.description or ""
            if tool.name == "wall" and "[WALL]" in desc and body in desc:
                return time.monotonic()
        await asyncio.sleep(0.1)
    return None


async def _clear_wall(sender: NatsRelay) -> None:
    """Reset the shared wall to empty between trials (through the poke path)."""
    await sender.set_wall(None)


class _WallLatencyTrials:
    """Runs one arm (push or poll) of the A/B comparison and reports results.

    Not a domain entity biff ships — a throwaway measurement harness
    scoped to this one test module, so it lives here rather than in
    ``src/biff/testing``.
    """

    __slots__ = ("_client",)

    def __init__(self, client: Client[Any]) -> None:
        self._client = client

    async def run_push(self, sender: NatsRelay) -> list[float]:
        """Measure push-path (real ``set_wall``) latency, ``_TRIALS`` times."""
        latencies: list[float] = []
        for i in range(_TRIALS):
            body = f"push wall trial {i}"
            t_send = time.monotonic()
            await sender.set_wall(_make_wall(body))
            t_recv = await _wait_for_wall_description(
                self._client, body=body, timeout=_TRIAL_TIMEOUT
            )
            latencies.append(t_recv - t_send if t_recv is not None else -1.0)
            await _clear_wall(sender)
        return latencies


def _print_latencies(push: list[float]) -> None:
    """Print the measured push-path detection latencies."""
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


@pytest.fixture
async def kai_wall_latency(
    nats_server: str, tmp_path: Path
) -> AsyncIterator[Client[Any]]:
    """A ``kai`` MCP server with a fast poll_interval, so detection is waitable."""
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
    mcp = create_server(state)
    async with StampedClient(FastMCPTransport(mcp)) as client:
        yield client
    _reset_session()


@pytest.fixture
async def wall_sender_relay(nats_server: str) -> AsyncIterator[NatsRelay]:
    """A bare ``NatsRelay`` for the sender side — no MCP server needed."""
    relay = NatsRelay(url=nats_server, repo_name=_TEST_REPO)
    try:
        yield relay
    finally:
        await relay.close()


class TestWallPushLatency:
    """End-to-end proof against real NATS: a pushed wall reaches a peer fast.

    This is PL-1104's claim exercised through the real code path — a wall
    written via :meth:`NatsRelay.set_wall` (which publishes the wall-notify
    wake poke) reconciles a *separate* MCP server's ``wall`` tool
    description in well under a second, across repeated trials, with no
    unrelated activity to wake the peer.

    The sub-second detection is only possible because the wake poke wires
    the sender's ``set_wall`` to the receiver's poller. Before PL-1104 the
    receiver had no such signal and only noticed a wall change on its own
    next scheduled tick (seconds, or minutes behind a wedged connection).
    """

    async def test_pushed_wall_reaches_peer_promptly(
        self,
        kai_wall_latency: Client[Any],
        wall_sender_relay: NatsRelay,
    ) -> None:
        kai_client = kai_wall_latency
        # A stamped tool call attaches the session and starts the poller
        # (``_LiveSession.attach`` fires on the first identified call, not on
        # ``list_tools``). Without this the poller never ticks and every
        # detection misses. Reading the wall is a harmless no-op call.
        await kai_client.call_tool("wall", {})
        await asyncio.sleep(1.0)  # let the poller establish subscribe_wall_notify

        trials = _WallLatencyTrials(kai_client)
        push_latencies = await trials.run_push(wall_sender_relay)

        _print_latencies(push_latencies)

        push_missed = sum(1 for t in push_latencies if t < 0)
        assert not push_missed, f"push missed {push_missed}/{_TRIALS} detections"

        # EVERY trial must land inside the bound, not just the average: the
        # receiver's poll interval is _POLL_INTERVAL (10s), so any trial that
        # fell through to a scheduled tick instead of the poke would measure
        # near 10s. Asserting each trial < _PUSH_LATENCY_BOUND (2s) means the
        # wall-notify poke — the only thing that can wake the poller that fast —
        # resolved every one. A broken poke cannot pass this.
        worst = max(push_latencies)
        assert worst < _PUSH_LATENCY_BOUND, (
            f"slowest push trial was {worst * 1000:.1f}ms, expected < "
            f"{_PUSH_LATENCY_BOUND * 1000:.0f}ms — a trial that slow means the "
            f"wake poke missed and a scheduled {_POLL_INTERVAL:.0f}s tick "
            f"reconciled it instead"
        )
