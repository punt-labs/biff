"""A harness session ends through biffd: the four effects, then the record.

The obligations are those of ``docs/specs/claude-session-end.tex``: no row, tty
name or record outlives the end (``EndInv``), an end loses no mail (the held
batch is filed back first), only a connection of the current run can end the
session (``EndRefused``), and a clear ends the old session before the new hello.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, cast

import pytest

from biff.client.connection import DaemonError
from biff.frame_reply import ErrorCode
from biff.relay import LocalRelay

if TYPE_CHECKING:
    from biff.client.connection import DaemonClient
    from biff.models import SessionEvent
    from tests.test_daemon.conftest import Harness


class LoggingRelay(LocalRelay):
    """A local relay that keeps the session history it is given, as NATS does."""

    events: ClassVar[list[SessionEvent]] = []

    async def append_wtmp(self, event: SessionEvent) -> None:
        LoggingRelay.events.append(event)


@pytest.fixture(autouse=True)
def logging_relay(harness: Harness) -> None:
    LoggingRelay.events = []
    harness.factory.relay_type = LoggingRelay


class Seat:
    """One harness session, and a second view of the relay it lives on."""

    def __init__(self, client: DaemonClient, viewer: LocalRelay, key: str, tty: str):
        self.client = client
        self.viewer = viewer
        self.key = key
        self.tty = tty

    async def row_present(self) -> bool:
        return await self.viewer.get_session(self.key) is not None

    async def owner(self) -> str | None:
        return await self.viewer.get_tty_reservation_owner("kai", self.tty)


async def _seat(harness: Harness, session_id: str, run: str = "a1") -> Seat:
    client = await harness.connect(session_id, "kai", client="mcp", main_agent_id=run)
    status = cast("dict[str, dict[str, str]]", await client.request("status"))
    viewer = LocalRelay(harness.factory.built[0])
    return Seat(
        client, viewer, status["data"]["session_key"], status["data"]["tty_name"]
    )


async def test_ending_a_session_removes_its_row_and_frees_its_name(
    harness: Harness,
) -> None:
    seat = await _seat(harness, "s-end")
    assert (await seat.row_present(), await seat.owner()) == (True, seat.key)
    await seat.client.request("end")
    assert (await seat.row_present(), await seat.owner()) == (False, None)


async def test_ending_a_session_appends_a_logout(harness: Harness) -> None:
    seat = await _seat(harness, "s-logout")
    await seat.client.request("end")
    logouts = [e for e in LoggingRelay.events if e.event == "logout"]
    assert [e.session_key for e in logouts] == [seat.key]


async def test_ending_a_session_forgets_its_record(harness: Harness) -> None:
    seat = await _seat(harness, "s-forget")
    await seat.client.request("end")
    assert harness.daemon.status().sessions == ()


async def test_an_ended_session_answers_no_more_requests(harness: Harness) -> None:
    seat = await _seat(harness, "s-after")
    await seat.client.request("end")
    with pytest.raises(DaemonError) as caught:
        await seat.client.request("read")
    assert caught.value.code is ErrorCode.SESSION_ENDED
