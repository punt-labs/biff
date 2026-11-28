"""``ArrivalPoll`` against a relay that fails and a client that leaves."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

from biff.daemon.arrival_poll import ArrivalPoll
from biff.daemon.refusal import ClientGoneError
from biff.nats_missing import StoreMissingError

if TYPE_CHECKING:
    from pydantic import JsonValue

    from biff.daemon.sessions import Session
    from biff.frames import EventKind


class _Relay:
    """An inbox whose high-water mark the test raises, and whose reads can fail."""

    def __init__(self) -> None:
        self.mark = 0
        self.fail: Exception | None = None

    async def inbox_high_water(self, _key: str) -> int:
        if self.fail is not None:
            raise self.fail
        return self.mark

    async def get_unread_summary(self, _key: str) -> SimpleNamespace:
        return SimpleNamespace(count=self.mark)

    async def get_wall(self) -> None:
        return None


class _Client:
    """The client end: records events and can be made to have left."""

    def __init__(self) -> None:
        self.events: list[tuple[str, JsonValue]] = []
        self.gone = False

    async def emit(self, kind: EventKind, data: JsonValue) -> None:
        if self.gone:
            raise ClientGoneError
        self.events.append((kind, data))


def _poll(relay: _Relay, client: _Client, wake: asyncio.Event) -> ArrivalPoll:
    session = SimpleNamespace(
        context=SimpleNamespace(relay=relay, session_key="kai:t"), outbox=[]
    )
    return ArrivalPoll(cast("Session", session), client.emit, 0.01, wake)


async def _arrival_for_a_client_that_left(relay: _Relay, client: _Client) -> None:
    wake = asyncio.Event()
    task = asyncio.ensure_future(_poll(relay, client, wake).run())
    await asyncio.sleep(0.05)  # the summary is out
    client.gone = True
    relay.mark = 1
    wake.set()
    await asyncio.wait_for(task, 2)  # the poll ends; it does not raise


async def test_a_notice_for_a_client_that_left_ends_the_poll_quietly() -> None:
    relay, client = _Relay(), _Client()
    await _arrival_for_a_client_that_left(relay, client)
    assert [kind for kind, _ in client.events] == ["unread"]  # no degraded


async def test_an_undelivered_arrival_is_in_the_next_summary() -> None:
    relay, client = _Relay(), _Client()
    await _arrival_for_a_client_that_left(relay, client)
    fresh = _Client()
    again = asyncio.ensure_future(_poll(relay, fresh, asyncio.Event()).run())
    await asyncio.sleep(0.05)
    again.cancel()
    assert fresh.events[0] == ("unread", {"count": 1})


async def test_a_relay_outage_is_one_degraded_then_recovered() -> None:
    relay, client = _Relay(), _Client()
    task = asyncio.ensure_future(_poll(relay, client, asyncio.Event()).run())
    await asyncio.sleep(0.05)
    relay.fail = ConnectionError("relay down")
    await asyncio.sleep(0.1)
    relay.fail = None
    await asyncio.sleep(0.1)
    task.cancel()
    assert [kind for kind, _ in client.events] == ["unread", "degraded", "recovered"]


async def test_a_missing_stream_is_reported_degraded_by_name() -> None:
    relay, client = _Relay(), _Client()
    task = asyncio.ensure_future(_poll(relay, client, asyncio.Event()).run())
    await asyncio.sleep(0.05)
    relay.fail = StoreMissingError("stream biff-inbox is missing")
    await asyncio.sleep(0.1)
    task.cancel()
    assert ("degraded", {"reason": "StoreMissingError"}) in client.events
