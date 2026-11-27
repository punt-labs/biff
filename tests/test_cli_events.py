"""The event pump never leaves a loop waiting for a connection that died."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from biff.cli_events import EventPump
from biff.client.connection import ConnectionLostError
from biff.frames import Event

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient


class _Client:
    """A connection whose next event is whatever the test says."""

    def __init__(self, failure: BaseException) -> None:
        self._failure = failure

    async def next_event(self) -> Event:
        raise self._failure


async def _lost_with(failure: BaseException) -> tuple[str, bool]:
    pump = EventPump(cast("DaemonClient", _Client(failure)))
    pump.start()
    await asyncio.wait_for(pump.wake.wait(), timeout=2.0)
    reason = pump.lost
    await pump.stop()
    return reason, pump.wake.is_set()


async def test_a_lost_connection_wakes_the_loop_with_the_reason() -> None:
    assert await _lost_with(ConnectionLostError("biffd went away")) == (
        "biffd went away",
        True,
    )


async def test_an_unexpected_failure_also_wakes_the_loop_and_says_what_it_was() -> None:
    reason, woke = await _lost_with(ValueError("a frame this client cannot read"))
    assert woke
    assert "ValueError" in reason
    assert "a frame this client cannot read" in reason


class _Replay:
    """A connection that delivers the events it was given, then waits."""

    def __init__(self, events: list[Event]) -> None:
        self._events = events

    async def next_event(self) -> Event:
        if self._events:
            return self._events.pop(0)
        await asyncio.Event().wait()
        raise AssertionError


async def test_talk_events_can_be_discarded_while_mail_and_wall_events_stay() -> None:
    kinds = ["mail", "talk_line", "talk_line", "wall", "talk_end"]
    events = [
        Event.model_validate({"seq": n + 1, "kind": k}) for n, k in enumerate(kinds)
    ]
    pump = EventPump(cast("DaemonClient", _Replay(events)))
    pump.start()
    await asyncio.sleep(0.1)  # the replayed events are all in
    pump.discard_talk()
    assert [e.kind for e in pump.take()] == ["mail", "wall"]
    await pump.stop()
