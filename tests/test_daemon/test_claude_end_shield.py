"""An end that has started finishes, even when its caller goes away mid-way."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from biff.client.connection import ConnectionLostError
from tests.test_daemon.claude_seats import LoggingRelay, logging_relay, seat, until

if TYPE_CHECKING:
    import pytest

    from tests.test_daemon.conftest import Harness

_ = logging_relay  # the autouse fixture, imported so pytest finds it here


async def test_a_caller_that_disconnects_mid_end_does_not_strand_the_alias(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-shield")
    reached = asyncio.Event()
    release = asyncio.Event()
    real = LoggingRelay.delete_session

    async def gated(self: LoggingRelay, session_key: str) -> None:
        reached.set()
        await release.wait()
        await real(self, session_key)

    monkeypatch.setattr(LoggingRelay, "delete_session", gated)
    pending = asyncio.ensure_future(s.client.request("end"))
    await asyncio.wait_for(reached.wait(), timeout=2)

    await s.client.close()  # SIGTERM took the hook: the connection goes
    with contextlib.suppress(ConnectionLostError):
        await pending
    release.set()

    await until(lambda: harness.daemon.status().sessions == ())
    assert (await s.row_present(), await s.owner()) == (False, None)
    assert [e.session_key for e in LoggingRelay.events if e.event == "logout"] == [
        s.key
    ]
