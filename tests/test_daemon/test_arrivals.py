"""The arrivals reading is read-only, and a mail event carries the inbox mark.

``docs/specs/notification-mail-event.tex``: reading the mark and the count marks no
mail read (``unreadN + readN = arrivedN``); each ``mail`` event names the high-water
mark it announces, so a client orders what it was told.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import pytest

from biff.relay import LocalRelay
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient
    from biff.frames import Event
    from tests.test_daemon.conftest import Harness

_WAIT = 5.0


async def _arrivals(client: DaemonClient) -> dict[str, object]:
    reply = cast("dict[str, dict[str, object]]", await client.request("arrivals"))
    return reply["data"]


async def _next(client: DaemonClient) -> Event:
    return await asyncio.wait_for(client.next_event(), timeout=_WAIT)


async def test_arrivals_reports_the_mark_the_count_and_no_wall(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="one")
    await eric.request("write", to="kai", message="two")
    data = await _arrivals(kai)
    assert data["unread"] == 2
    assert cast("int", data["mark"]) >= 2
    assert data["wall"] is None
    assert (data["invites"], data["talk_pending"]) == ([], 0)


async def test_arrivals_marks_nothing_read(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="still unread")
    for _ in range(3):
        assert (await _arrivals(kai))["unread"] == 1
    rows = cast("dict[str, list[dict[str, str]]]", await kai.request("read"))
    assert [r["body"] for r in rows["data"]] == ["still unread"]


async def test_arrivals_marks_nothing_read_on_a_consuming_relay(
    harness: Harness,
) -> None:
    harness.factory.relay_type = ConsumingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="kept")
    assert (await _arrivals(kai))["unread"] == 1
    rows = cast("dict[str, list[dict[str, str]]]", await kai.request("read"))
    assert [r["body"] for r in rows["data"]] == ["kept"]


async def test_arrivals_counts_the_mail_the_daemon_holds(harness: Harness) -> None:
    harness.factory.relay_type = ConsumingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="fetched, not confirmed")
    await kai.request_held("read")  # the relay gave it up; the outbox holds it
    assert (await _arrivals(kai))["unread"] == 1


async def test_arrivals_carries_the_active_wall(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("wall", message="deploy freeze", duration="1h")
    wall = cast("dict[str, object]", (await _arrivals(kai))["wall"])
    assert wall["text"] == "deploy freeze"


async def test_a_mail_event_carries_the_inbox_mark(harness: Harness) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.subscribe()
    await _next(kai)
    before = cast("int", (await _arrivals(kai))["mark"])
    await eric.request("write", to="kai", message="new")
    event = await _next(kai)
    data = cast("dict[str, int]", event.data)
    assert event.kind == "mail"
    assert data["count"] == 1
    assert data["mark"] > before
    assert data["mark"] == (await _arrivals(kai))["mark"]


async def test_arrivals_refuses_params(harness: Harness) -> None:
    from biff.client.connection import DaemonError

    kai = await harness.connect("s-kai", "kai")
    with pytest.raises(DaemonError):
        await kai.request("arrivals", session_key="eric:abc")


async def test_arrivals_reports_whether_the_session_takes_messages(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    assert (await _arrivals(kai))["mesg"] is True

    await kai.request("mesg", value="n")

    assert (await _arrivals(kai))["mesg"] is False


async def test_an_invite_nobody_has_read_yet_is_listed_as_live(
    harness: Harness, bus: object
) -> None:
    assert bus is not None
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    status = cast("dict[str, dict[str, str]]", await kai.request("status"))
    await eric.request("talk", to=f"kai:{status['data']['tty_name']}", message="")
    await asyncio.sleep(0.2)  # the invite is queued, not yet drained by talk_read

    data = await _arrivals(kai)

    invites = cast("list[dict[str, str]]", data["invites"])
    assert [i["from"] for i in invites] == ["eric"]
    # The queued frame is the invite: it is a frame waiting, but not a talk line.
    assert (data["talk_pending"], data["talk_lines"]) == (1, 0)


async def test_a_session_whose_row_is_gone_is_logged_once_as_mesg_on(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    kai = await harness.connect("s-kai", "kai")
    key = cast("dict[str, dict[str, str]]", await kai.request("status"))["data"][
        "session_key"
    ]
    await LocalRelay(harness.factory.built[0]).delete_session(key)

    with caplog.at_level(logging.WARNING, logger="biff.daemon.arrivals_reading"):
        first = await _arrivals(kai)
        second = await _arrivals(kai)

    assert first["mesg"] is True and second["mesg"] is True
    assert caplog.text.count("has no session row") == 1
