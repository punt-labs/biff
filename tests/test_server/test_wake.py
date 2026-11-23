"""Tests for the WakeSignal the poller sleeps on."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from biff.models import Message
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.wake import WakeOutcome, WakeSignal

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


class TestWakeSignal:
    def test_starts_unset(self) -> None:
        assert not WakeSignal().is_set

    def test_wake_sets_the_signal(self) -> None:
        signal = WakeSignal()
        signal.wake()
        assert signal.is_set

    async def test_wakes_coalesce_into_one(self) -> None:
        signal = WakeSignal()
        signal.wake()
        signal.wake()
        assert await signal.wait(timeout=None, shutdown=None) is WakeOutcome.EVENT
        assert not signal.is_set

    async def test_a_waiter_resumes_on_wake(self) -> None:
        signal = WakeSignal()
        waiter = asyncio.create_task(signal.wait(timeout=None, shutdown=None))
        await asyncio.sleep(0)
        assert not waiter.done()
        signal.wake()
        async with asyncio.timeout(1.0):
            assert await waiter is WakeOutcome.EVENT

    async def test_shutdown_wins(self) -> None:
        signal = WakeSignal()
        shutdown = asyncio.Event()
        shutdown.set()
        outcome = await signal.wait(timeout=None, shutdown=shutdown)
        assert outcome is WakeOutcome.SHUTDOWN

    async def test_the_timeout_is_a_timeout_outcome(self) -> None:
        outcome = await WakeSignal().wait(timeout=0.01, shutdown=None)
        assert outcome is WakeOutcome.TIMEOUT


class TestLostWakeRace:
    """A ``wake()`` landing between ``asyncio.wait()``'s timeout decision and
    the signal's own clear must not be silently erased.

    ``asyncio.wait()``'s timeout resolution and a NATS callback's ``wake()``
    are two independent events that can interleave in either order; if
    ``done`` came back empty (the wait-task lost that narrow race) while the
    signal was, in fact, set, trusting ``done`` alone reports TIMEOUT instead
    of EVENT.  ``poll_inbox`` treats a TIMEOUT as "skip tick work", so the
    poke would be lost until the next wake, not merely delayed.
    """

    async def test_wake_set_despite_empty_done_still_reports_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        signal = WakeSignal()

        async def _fake_wait(
            waiters: list[asyncio.Future[object]],
            *,
            timeout: float | None,
            return_when: str,
        ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
            del waiters, timeout, return_when
            # asyncio.wait() decided "nothing completed" (empty done) in the
            # same window a wake() landed — reproducible without depending on
            # real scheduler timing.
            signal.wake()
            return set(), set()

        monkeypatch.setattr(asyncio, "wait", _fake_wait)

        outcome = await signal.wait(timeout=1000.0, shutdown=None)

        assert outcome is WakeOutcome.EVENT
        assert not signal.is_set  # still consumed before returning

    async def test_truly_empty_done_and_unset_signal_reports_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary TIMEOUT path must still work — this fix must not turn
        every wait into a false EVENT."""

        async def _fake_wait(
            waiters: list[asyncio.Future[object]],
            *,
            timeout: float | None,
            return_when: str,
        ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
            del waiters, timeout, return_when
            return set(), set()

        monkeypatch.setattr(asyncio, "wait", _fake_wait)

        outcome = await WakeSignal().wait(timeout=1000.0, shutdown=None)

        assert outcome is WakeOutcome.TIMEOUT


class TestWatching:
    """``watching`` is how a relay's arrivals wake the poller."""

    async def test_a_local_relay_arrival_wakes_the_signal(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path)
        signal = WakeSignal()
        with signal.watching(relay):
            await relay.deliver(Message(from_user="eric", to_user="kai", body="hi"))
            outcome = await signal.wait(timeout=15.0, shutdown=None)
        assert outcome is WakeOutcome.EVENT

    async def test_a_push_relay_has_nothing_to_watch(self) -> None:
        with WakeSignal().watching(NatsRelay(url="nats://localhost:4222")) as entered:
            assert entered is None
