"""The arrival subscription: current summary first, then one event per arrival."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from biff.frames import Event

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient
    from tests.test_daemon.conftest import Harness

_WAIT = 5.0


async def _next(events: DaemonClient) -> Event:
    return await asyncio.wait_for(events.next_event(), timeout=_WAIT)


async def test_subscribe_first_sends_the_current_unread_summary(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="while you were out")
    await kai.subscribe()
    event = await _next(kai)
    assert (event.kind, event.data) == ("unread", {"count": 1})


async def test_a_later_arrival_is_one_event_without_a_body(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.subscribe()
    await _next(kai)
    await eric.request("write", to="kai", message="secret body")
    event = await _next(kai)
    assert event.kind == "mail"
    assert "secret body" not in event.model_dump_json()


async def test_an_active_wall_follows_the_unread_summary(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("wall", message="deploy freeze", duration="1h")
    await kai.subscribe()
    kinds = [(await _next(kai)).kind, (await _next(kai)).kind]
    assert kinds == ["unread", "wall"]


async def test_event_sequence_numbers_rise(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.subscribe()
    first = await _next(kai)
    await eric.request("write", to="kai", message="x")
    second = await _next(kai)
    assert second.seq > first.seq


async def test_a_second_client_attaching_does_not_disturb_a_subscription(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.subscribe()
    await _next(kai)
    await harness.connect("s-other", "kai")
    await harness.connect("s-kai", "kai", client="hook")
    await eric.request("write", to="kai", message="still delivered")
    assert (await _next(kai)).kind == "mail"


async def test_a_new_run_releases_the_old_runs_subscription(harness: Harness) -> None:
    old = await harness.connect("s-kai", "kai", main_agent_id="run-1")
    eric = await harness.connect("s-eric", "eric")
    await old.subscribe()
    await _next(old)
    new = await harness.connect("s-kai", "kai", main_agent_id="run-2")
    await new.subscribe()
    await _next(new)
    await eric.request("write", to="kai", message="for the new run")
    assert (await _next(new)).kind == "mail"
    assert old.pending_events == 0
