"""Work a cancelled caller cannot stop still finishes, and its failure is reported."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from biff.daemon.uncancellable import Uncancellable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from biff.daemon.sessions import Session
    from tests.test_daemon.conftest import Harness

_WAIT = 3.0


async def test_cancelling_the_caller_does_not_stop_the_work() -> None:
    release, finished = asyncio.Event(), asyncio.Event()

    async def work() -> str:
        await release.wait()
        finished.set()
        return "done"

    caller = asyncio.create_task(Uncancellable.run(work(), "work"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    release.set()
    await asyncio.wait_for(finished.wait(), _WAIT)


async def test_a_failure_after_the_caller_left_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    release = asyncio.Event()

    async def work() -> str:
        await release.wait()
        msg = "relay went away"
        raise OSError(msg)

    caller = asyncio.create_task(Uncancellable.run(work(), "orphan"))
    await asyncio.sleep(0)
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    with caplog.at_level(logging.ERROR, logger="biff.daemon.uncancellable"):
        release.set()
        async with asyncio.timeout(_WAIT):
            while "orphan failed after its caller left" not in caplog.text:
                await asyncio.sleep(0.01)


async def test_a_waiting_caller_receives_the_result() -> None:
    async def work() -> str:
        return "result"

    assert await Uncancellable.run(work(), "work") == "result"


async def test_a_failure_the_cancel_beat_to_the_caller_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def work() -> str:
        msg = "relay went away"
        raise OSError(msg)

    caller = asyncio.create_task(Uncancellable.run(work(), "raced"))
    await asyncio.sleep(0)  # the caller waits on the work
    await asyncio.sleep(0)  # the work fails; the caller has not yet been told
    caller.cancel()  # the cancel is delivered in place of the failure
    with caplog.at_level(logging.ERROR, logger="biff.daemon.uncancellable"):
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert "raced failed after its caller left" in caplog.text


async def test_a_read_that_gave_up_before_taking_the_session_never_runs(
    harness: Harness,
) -> None:
    await harness.connect("s-kai", "kai")
    (session,) = harness.daemon._core.sessions.snapshot().values()  # pyright: ignore[reportPrivateUsage]
    release = asyncio.Event()
    ran: list[str] = []

    def reading(name: str) -> Callable[[Session], Awaitable[None]]:
        async def read(_session: Session) -> None:
            ran.append(name)
            if name == "slow":
                await release.wait()

        return read

    async def parked(count: int) -> None:
        async with asyncio.timeout(_WAIT):
            while session.queued < count:
                await asyncio.sleep(0.005)

    slow = asyncio.create_task(Uncancellable.holding(session, reading("slow"), "r"))
    await parked(1)
    expired = asyncio.create_task(
        Uncancellable.holding(session, reading("expired"), "r")
    )
    await parked(2)
    expired.cancel()  # the request timed out while waiting for the session
    with pytest.raises(asyncio.CancelledError):
        await expired
    live = asyncio.create_task(Uncancellable.holding(session, reading("live"), "r"))
    await parked(2)
    release.set()
    await asyncio.wait_for(asyncio.gather(slow, live), _WAIT)
    assert ran == ["slow", "live"]
