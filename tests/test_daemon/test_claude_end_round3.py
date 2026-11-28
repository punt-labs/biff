"""More hazards of ending a harness session: a capped fetch, a caller that leaves."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, ClassVar, cast

import pytest

from biff.models import Message
from biff.relay import LocalRelay
from tests.test_daemon.claude_seats import (
    LoggingRelay,
    logging_relay,
    seat,
    until,
)
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_ = logging_relay


class CappedRelay(ConsumingRelay):
    """A consuming relay whose fetch returns at most ``cap`` messages, as NATS does."""

    cap: ClassVar[int] = 100

    async def fetch(self, session_key: str) -> list[Message]:
        batch = (await LocalRelay.fetch(self, session_key))[: CappedRelay.cap]
        await LocalRelay.mark_read(self, session_key, [m.id for m in batch])
        return batch


class SlowForwardingRelay(CappedRelay):
    """A capped relay whose every publish takes 3 ms, as a loaded server's does."""

    async def deliver(self, message: Message) -> None:
        await asyncio.sleep(0.003)
        await super().deliver(message)


# The test is about volume, not the timeout: 250 forwards at 3 ms take 0.75 s,
# over the harness's default 0.5 s request budget, so it sets its own.
@pytest.mark.parametrize("harness", [10.0], indirect=True)
async def test_an_end_forwards_more_mail_than_one_fetch_returns(
    harness: Harness,
) -> None:
    harness.factory.relay_type = SlowForwardingRelay
    s = await seat(harness, "s-many")
    for n in range(250):
        await s.viewer.deliver(
            Message(from_user="eric", to_user=s.key, body=f"m{n:03d}")
        )
    await s.client.request("end")
    mailbox = {m.body for m in await s.viewer.fetch_user_inbox("kai")}
    assert mailbox == {f"m{n:03d}" for n in range(250)}


async def test_a_caller_that_leaves_while_the_end_waits_leaves_the_session_open(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    ender = await seat(harness, "s-leaves")
    other = await harness.connect("s-leaves", "kai", client="mcp", main_agent_id="a1")
    parked, release = asyncio.Event(), asyncio.Event()
    real = LoggingRelay.update_session_if

    async def parks_once(self: LoggingRelay, edit: object) -> object:
        if not parked.is_set():
            parked.set()
            await release.wait()
        return await real(self, edit)  # type: ignore[arg-type]

    monkeypatch.setattr(LoggingRelay, "update_session_if", parks_once)
    plan = asyncio.ensure_future(other.request("plan", message="running"))
    await parked.wait()
    end = asyncio.ensure_future(ender.client.request("end"))
    table = harness.daemon._core.sessions
    # The end has begun once the session refuses new requests; it waits for the plan.
    await until(lambda: any(v._hold._ending for v in table.snapshot().values()))
    await ender.client.close()  # the caller leaves while its end waits for the plan
    await asyncio.gather(end, return_exceptions=True)
    release.set()
    await plan
    again = cast("dict[str, object]", await other.request("who"))
    assert again["error"] is False  # a refusal here would be session_ended for ever
    assert await ender.row_present() is True
