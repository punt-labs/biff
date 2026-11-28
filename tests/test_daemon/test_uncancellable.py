"""Work a cancelled caller cannot stop still finishes, and its failure is reported."""

from __future__ import annotations

import asyncio
import logging

import pytest

from biff.daemon.uncancellable import Uncancellable

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
