"""Latency comparison: KV watch vs NATS core subscription.

Diagnostic test for biff-8g0 — talk push notifications not reaching
MCP session from CLI client while wall push works.  Isolates whether
the latency difference is in NATS delivery or the MCP callback chain.

Both channels are tested at the relay level (no MCP/FastMCP involved)
to rule out transport-layer issues first.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING

import nats
import pytest

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-latency"
_TRIALS = 5


async def _measure_kv_latency(
    nc_pub: NatsClient, nc_sub: NatsClient, repo: str
) -> list[float]:
    """Measure KV watch delivery latency.

    Mimics the wall path: one client writes a KV entry, another
    client's KV watcher receives the change event.

    Uses ``watcher.updates()`` instead of ``async for`` because
    nats.py's ``__anext__`` raises ``StopAsyncIteration`` on the
    snapshot-done ``None`` marker, terminating the iterator.
    """
    js_pub = nc_pub.jetstream()  # pyright: ignore[reportUnknownMemberType]
    js_sub = nc_sub.jetstream()  # pyright: ignore[reportUnknownMemberType]

    bucket_name = "biff-latency-test"
    try:
        kv_pub = await js_pub.key_value(bucket_name)  # pyright: ignore[reportUnknownMemberType]
    except nats.js.errors.BucketNotFoundError:  # pyright: ignore[reportUnknownMemberType,reportAttributeAccessIssue]
        kv_pub = await js_pub.create_key_value(bucket=bucket_name)  # pyright: ignore[reportUnknownMemberType]
    kv_sub = await js_sub.key_value(bucket_name)  # pyright: ignore[reportUnknownMemberType]

    key = f"{repo}.wall"
    await kv_pub.put(key, b"init")

    watcher = await kv_sub.watchall()  # pyright: ignore[reportUnknownMemberType]

    received = asyncio.Event()
    t_recv = 0.0
    init_done = False

    async def _watch_loop() -> None:
        nonlocal t_recv, init_done
        while True:
            entry = await watcher.updates(timeout=30.0)  # type: ignore[no-untyped-call]
            if entry is None:
                init_done = True
                continue
            if str(entry.key) != key:  # pyright: ignore[reportUnknownMemberType,reportUnknownArgumentType,reportAttributeAccessIssue]
                continue
            if not init_done:
                continue
            t_recv = time.monotonic()
            received.set()

    watch_task = asyncio.create_task(_watch_loop())
    await asyncio.sleep(0.1)  # let snapshot drain

    latencies: list[float] = []
    for i in range(_TRIALS):
        received.clear()
        payload = json.dumps({"trial": i, "ts": time.time()}).encode()
        t_send = time.monotonic()
        await kv_pub.put(key, payload)

        try:
            await asyncio.wait_for(received.wait(), timeout=5.0)
            latencies.append(t_recv - t_send)
        except TimeoutError:
            latencies.append(-1.0)

    watch_task.cancel()
    await watcher.stop()  # type: ignore[no-untyped-call]
    return latencies


async def _measure_core_nats_latency(
    nc_pub: NatsClient, nc_sub: NatsClient, repo: str
) -> list[float]:
    """Measure NATS core pub/sub delivery latency.

    Mimics the talk path: one client publishes to a core NATS subject,
    another client's subscription callback receives it.  Single
    subscription for all trials (matches MCP server behaviour).
    """
    subject = f"biff.{repo}.talk.notify.testuser"

    received = asyncio.Event()
    t_recv = 0.0

    async def _on_msg(msg: object) -> None:
        nonlocal t_recv
        t_recv = time.monotonic()
        received.set()

    sub = await nc_sub.subscribe(subject, cb=_on_msg)  # pyright: ignore[reportUnknownMemberType]
    await asyncio.sleep(0.05)  # let subscription propagate

    latencies: list[float] = []
    for i in range(_TRIALS):
        received.clear()
        payload = json.dumps({"trial": i, "ts": time.time()}).encode()
        t_send = time.monotonic()
        await nc_pub.publish(subject, payload)

        try:
            await asyncio.wait_for(received.wait(), timeout=5.0)
            latencies.append(t_recv - t_send)
        except TimeoutError:
            latencies.append(-1.0)

    await sub.unsubscribe()
    return latencies


@pytest.mark.nats
class TestNotificationLatency:
    """Compare KV watch vs NATS core subscription delivery latency."""

    async def test_kv_watch_delivers(self, nats_server: str) -> None:
        """KV watch notifications arrive within 500ms (wall path)."""
        nc1 = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        nc2 = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            latencies = await _measure_kv_latency(nc1, nc2, _TEST_REPO)
            missed = [t for t in latencies if t < 0]
            assert not missed, f"KV watch missed {len(missed)}/{_TRIALS} notifications"
            avg_ms = sum(latencies) * 1000 / len(latencies)
            max_ms = max(latencies) * 1000
            print(f"\nKV watch: avg={avg_ms:.1f}ms  max={max_ms:.1f}ms")
            assert max_ms < 500, f"KV watch too slow: max {max_ms:.1f}ms"
        finally:
            await nc1.close()
            await nc2.close()

    async def test_core_nats_delivers(self, nats_server: str) -> None:
        """NATS core subscription notifications arrive within 500ms (talk path)."""
        nc1 = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        nc2 = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            latencies = await _measure_core_nats_latency(nc1, nc2, _TEST_REPO)
            missed = [t for t in latencies if t < 0]
            assert not missed, f"Core NATS missed {len(missed)}/{_TRIALS} notifications"
            avg_ms = sum(latencies) * 1000 / len(latencies)
            max_ms = max(latencies) * 1000
            print(f"\nCore NATS: avg={avg_ms:.1f}ms  max={max_ms:.1f}ms")
            assert max_ms < 500, f"Core NATS too slow: max {max_ms:.1f}ms"
        finally:
            await nc1.close()
            await nc2.close()

    async def test_latency_comparison(self, nats_server: str) -> None:
        """Side-by-side comparison: KV watch vs core NATS delivery."""
        nc_pub = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        nc_sub = await nats.connect(nats_server)  # pyright: ignore[reportUnknownMemberType]
        try:
            kv = await _measure_kv_latency(nc_pub, nc_sub, _TEST_REPO)
            core = await _measure_core_nats_latency(nc_pub, nc_sub, _TEST_REPO)

            kv_missed = sum(1 for t in kv if t < 0)
            core_missed = sum(1 for t in core if t < 0)

            kv_valid = [t for t in kv if t >= 0]
            core_valid = [t for t in core if t >= 0]

            print(f"\n{'Channel':<12} {'Avg (ms)':>10} {'Max (ms)':>10} {'Missed':>8}")
            print("-" * 44)
            if kv_valid:
                print(
                    f"{'KV watch':<12} "
                    f"{sum(kv_valid) * 1000 / len(kv_valid):>10.1f} "
                    f"{max(kv_valid) * 1000:>10.1f} "
                    f"{kv_missed:>8}"
                )
            if core_valid:
                print(
                    f"{'Core NATS':<12} "
                    f"{sum(core_valid) * 1000 / len(core_valid):>10.1f} "
                    f"{max(core_valid) * 1000:>10.1f} "
                    f"{core_missed:>8}"
                )

            # Both should deliver — if core misses but KV doesn't,
            # the problem is in NATS core delivery, not the MCP layer.
            if core_missed > 0 and kv_missed == 0:
                pytest.fail(
                    f"Core NATS missed {core_missed}/{_TRIALS} but "
                    f"KV watch missed none — problem is in NATS delivery"
                )
        finally:
            await nc_pub.close()
            await nc_sub.close()
