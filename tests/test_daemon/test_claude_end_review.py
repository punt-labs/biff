"""Hazards of ending a harness session that a review found, each forced by a barrier.

No test sleeps for a race: a relay parks the step under test until the test has
sent the competing request, or flips the state the step is about to rely on.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, cast

import pytest

from biff.client.connection import DaemonError
from biff.daemon.session_key import SessionKey
from biff.frame_reply import ErrorCode
from biff.models import Message
from biff.session_id import CLAUDE_CODE_ROLE
from tests.test_daemon.claude_seats import (
    ConsumingLogging,
    LoggingRelay,
    logging_relay,
    seat,
    until,
)

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_ = logging_relay


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("mesg", {"value": "n"}),
        ("tty", {"name": "revived"}),
        ("plan", {"message": "x"}),
    ],
)
async def test_every_request_of_an_ended_session_is_refused(
    harness: Harness, method: str, params: dict[str, str]
) -> None:
    ender = await seat(harness, "s-dead")
    other = await harness.connect("s-dead", "kai", client="mcp", main_agent_id="a1")
    await ender.client.request("end")
    with pytest.raises(DaemonError) as caught:
        await other.request(method, **params)
    assert caught.value.code is ErrorCode.SESSION_ENDED
    assert await ender.row_present() is False


async def test_a_request_in_flight_when_the_end_begins_cannot_recreate_the_row(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ender = await seat(harness, "s-flight")
    other = await harness.connect("s-flight", "kai", client="mcp", main_agent_id="a1")
    parked, release = asyncio.Event(), asyncio.Event()
    real = LoggingRelay.update_session_if

    async def parks_once(self: LoggingRelay, edit: object) -> object:
        if not parked.is_set():
            parked.set()
            await release.wait()
        return await real(self, edit)  # type: ignore[arg-type]

    monkeypatch.setattr(LoggingRelay, "update_session_if", parks_once)
    plan = asyncio.ensure_future(other.request("plan", message="late"))
    await parked.wait()
    end = asyncio.ensure_future(ender.client.request("end"))
    # An end must wait for the request already running; an end that did not
    # would finish here, and the plan would then rebuild the row it deleted.
    await asyncio.wait({end}, timeout=0.3)
    release.set()
    await asyncio.gather(plan, end, return_exceptions=True)
    assert await ender.row_present() is False


class DroppingRelay(ConsumingLogging):
    """A consuming relay whose forwards fail while it is told the network is down."""

    down: ClassVar[bool] = False

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if DroppingRelay.down:
            msg = "relay dropped"
            raise ConnectionError(msg)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


async def test_mail_fetched_by_an_end_that_then_fails_reaches_the_user_mailbox(
    harness: Harness,
) -> None:
    harness.factory.relay_type = DroppingRelay
    DroppingRelay.down = False
    s = await seat(harness, "s-drop")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="keep me")
    DroppingRelay.down = True  # the relay drops after the consuming fetch
    with pytest.raises(DaemonError):
        await s.client.request("end")
    DroppingRelay.down = False
    await s.client.request("end")
    mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
    assert mailbox == ["keep me"]


class GapRelay(LoggingRelay):
    """A relay where a writer that resolved the row first delivers after it went."""

    gap_body: ClassVar[str] = ""

    async def delete_session(self, session_key: str) -> None:
        await super().delete_session(session_key)
        if GapRelay.gap_body:
            body, GapRelay.gap_body = GapRelay.gap_body, ""
            await self.deliver(
                Message(from_user="eric", to_user=session_key, body=body)
            )


async def test_mail_accepted_while_the_row_is_removed_reaches_the_user_mailbox(
    harness: Harness,
) -> None:
    harness.factory.relay_type = GapRelay
    s = await seat(harness, "s-gap")
    GapRelay.gap_body = "in the gap"
    await s.client.request("end")
    mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
    assert mailbox == ["in the gap"]


async def test_a_queued_end_after_a_queued_hello_does_not_end_the_reopened_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = await seat(harness, "s-straddle", run="a1")
    other = await harness.connect("s-straddle", "kai", client="mcp", main_agent_id="a1")
    parked, release = asyncio.Event(), asyncio.Event()
    real = LoggingRelay.delete_session

    async def parks_once(self: LoggingRelay, session_key: str) -> None:
        if not parked.is_set():
            parked.set()
            await release.wait()
        await real(self, session_key)

    monkeypatch.setattr(LoggingRelay, "delete_session", parks_once)
    table = harness.daemon._core.sessions
    key = SessionKey("s-straddle", CLAUDE_CODE_ROLE)
    ending = asyncio.ensure_future(first.client.request("end"))
    await parked.wait()
    hello = asyncio.ensure_future(
        harness.connect("s-straddle", "kai", client="mcp", main_agent_id="a2")
    )
    await until(lambda: table._users[key] == 2)  # the hello is queued
    stale = asyncio.ensure_future(other.request("end"))
    await until(lambda: table._users[key] == 3)  # and the second end behind it
    release.set()
    reopened = await hello
    for reply in await asyncio.gather(ending, stale):  # both succeed, one doing nothing
        assert cast("dict[str, object]", reply)["error"] is False
    status = cast("dict[str, dict[str, str]]", await reopened.request("status"))
    assert await first.viewer.get_session(status["data"]["session_key"]) is not None
    assert len(harness.daemon.status().sessions) == 1


class FlippingRelay(LoggingRelay):
    """A relay whose tty name changes hands just before it is released."""

    newcomer: ClassVar[str] = ""

    async def _flip(self, user: str, name: str) -> None:
        if FlippingRelay.newcomer:
            key, FlippingRelay.newcomer = FlippingRelay.newcomer, ""
            await super().release_tty_name(user, name)
            await super().reserve_tty_name(user, name, key)

    async def release_tty_name(self, user: str, name: str) -> None:
        await self._flip(user, name)
        await super().release_tty_name(user, name)

    async def release_tty_name_if(self, user: str, name: str, owner: str) -> bool:
        await self._flip(user, name)
        return await super().release_tty_name_if(user, name, owner)


async def test_an_end_never_frees_a_name_that_changed_hands_after_the_check(
    harness: Harness,
) -> None:
    harness.factory.relay_type = FlippingRelay
    s = await seat(harness, "s-flip")
    FlippingRelay.newcomer = "kai:newcomer"
    await s.client.request("end")
    assert await s.owner() == "kai:newcomer"
