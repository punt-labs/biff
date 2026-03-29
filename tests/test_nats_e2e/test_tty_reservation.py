"""NATS E2E tests for TTY name reservation (DES-035).

Tests the atomicity guarantees of NATS KV ``create()`` for TTY name
reservation, release-then-reuse, concurrent claim, and heartbeat refresh.
"""

from __future__ import annotations

import asyncio

import pytest

from biff.nats_relay import NatsRelay
from biff.tty import claim_tty_name

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-tty-reservation"
_prefix_counter = 0


def _unique_prefix() -> str:
    """Return a unique stream prefix to isolate each test's NATS state."""
    global _prefix_counter
    _prefix_counter += 1
    return f"biff-ttytest{_prefix_counter}"


async def _make_relay(
    nats_server: str, *, name: str = "test-tty", prefix: str | None = None
) -> NatsRelay:
    """Create a NatsRelay instance with an isolated stream prefix."""
    return NatsRelay(
        url=nats_server,
        name=name,
        repo_name=_TEST_REPO,
        stream_prefix=prefix or _unique_prefix(),
    )


class TestConcurrentReservation:
    """Two relay instances race to reserve the same TTY name."""

    async def test_one_wins_one_loses(self, nats_server: str) -> None:
        """Concurrent reserve_tty_name on same name: one True, one False."""
        prefix = _unique_prefix()
        relay_a = await _make_relay(nats_server, name="relay-a", prefix=prefix)
        relay_b = await _make_relay(nats_server, name="relay-b", prefix=prefix)

        try:
            result_a, result_b = await asyncio.gather(
                relay_a.reserve_tty_name("kai", "tty1", "kai:sess-a"),
                relay_b.reserve_tty_name("kai", "tty1", "kai:sess-b"),
            )
            # Exactly one should win
            assert result_a != result_b
            assert result_a or result_b
        finally:
            await relay_a.delete_infrastructure()
            await relay_a.close()
            await relay_b.close()


class TestReleaseAndReuse:
    """Reserve, release, re-reserve the same name."""

    async def test_release_then_reserve_succeeds(self, nats_server: str) -> None:
        """After release, the same name can be reserved again."""
        relay = await _make_relay(nats_server)

        try:
            ok = await relay.reserve_tty_name("kai", "tty1", "kai:sess1")
            assert ok is True

            await relay.release_tty_name("kai", "tty1")

            ok = await relay.reserve_tty_name("kai", "tty1", "kai:sess2")
            assert ok is True
        finally:
            await relay.delete_infrastructure()
            await relay.close()


class TestListReservedNames:
    """list_reserved_names returns accurate state."""

    async def test_list_reflects_reservations(self, nats_server: str) -> None:
        """Reserve two names, verify both appear in the list."""
        relay = await _make_relay(nats_server)

        try:
            await relay.reserve_tty_name("kai", "tty1", "kai:sess1")
            await relay.reserve_tty_name("kai", "tty3", "kai:sess1")

            names = await relay.list_reserved_names("kai")
            assert sorted(names) == ["tty1", "tty3"]
        finally:
            await relay.delete_infrastructure()
            await relay.close()


class TestClaimNoCollision:
    """Two relays each call claim_tty_name and get distinct names."""

    async def test_concurrent_claim_distinct_names(self, nats_server: str) -> None:
        """Two relay instances claiming for the same user get different names."""
        prefix = _unique_prefix()
        relay_a = await _make_relay(nats_server, name="relay-a", prefix=prefix)
        relay_b = await _make_relay(nats_server, name="relay-b", prefix=prefix)

        try:
            name_a, name_b = await asyncio.gather(
                claim_tty_name(relay_a, "kai", "kai:sess-a"),
                claim_tty_name(relay_b, "kai", "kai:sess-b"),
            )

            assert name_a != name_b
            assert name_a.startswith("tty")
            assert name_b.startswith("tty")
        finally:
            await relay_a.delete_infrastructure()
            await relay_a.close()
            await relay_b.close()


class TestHeartbeatRefresh:
    """Heartbeat refresh keeps reservation alive."""

    async def test_refresh_preserves_reservation(self, nats_server: str) -> None:
        """After refresh, the name is still in the reserved list."""
        relay = await _make_relay(nats_server)

        try:
            await relay.reserve_tty_name("kai", "tty1", "kai:sess1")

            await relay.refresh_tty_reservation("kai", "tty1", "kai:sess1")

            names = await relay.list_reserved_names("kai")
            assert "tty1" in names
        finally:
            await relay.delete_infrastructure()
            await relay.close()
