"""A daemon that stops hands the mail it holds back to the relay."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.handback import Handback
from biff.daemon.server import BiffDaemon
from biff.run_dir import RunDir
from tests.test_daemon.conftest import CountingFactory, FakeResolver
from tests.test_daemon.consuming import ConsumingRelay

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from biff.models import Message
    from tests.test_daemon.conftest import Harness

_WAIT = 3.0


class FetchingRelay(ConsumingRelay):
    """A consuming relay whose broadcast fetch says it took the mail, then stalls."""

    consumed = asyncio.Event()
    delay = 0.3

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        unread = await super().fetch_user_inbox(user)
        type(self).consumed.set()
        await asyncio.sleep(type(self).delay)
        return unread


class FlakyRelay(ConsumingRelay):
    """A consuming relay whose next ``deliver`` calls can be made to fail."""

    failing = 0

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if type(self).failing:
            type(self).failing -= 1
            msg = "the relay refused the publish"
            raise OSError(msg)
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


class StalledPublishRelay(ConsumingRelay):
    """A consuming relay whose publishes can be made to never return."""

    stalled = False

    async def deliver(
        self,
        message: Message,
        *,
        sender_key: str = "",
        target_repo: str | None = None,
    ) -> None:
        if type(self).stalled:
            await asyncio.Event().wait()  # never; the caller's deadline cancels it
        await super().deliver(message, sender_key=sender_key, target_repo=target_repo)


class StuckRelay(ConsumingRelay):
    """A consuming relay whose broadcast fetch can hang before it takes any mail."""

    stuck = asyncio.Event()
    release = asyncio.Event()
    hanging = False

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        if type(self).hanging:
            type(self).stuck.set()
            await type(self).release.wait()
        return await super().fetch_user_inbox(user)


class BroadcastStuckRelay(ConsumingRelay):
    """A consuming relay whose broadcast fetch hangs once the session fetch is done."""

    stuck = asyncio.Event()
    release = asyncio.Event()

    async def fetch_user_inbox(self, user: str) -> list[Message]:
        type(self).stuck.set()
        await type(self).release.wait()
        return await super().fetch_user_inbox(user)


def _bodies(result: object) -> list[str]:
    rows = cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])
    return [row["body"] for row in rows]


async def _read_after_restart(harness: Harness, tmp_path: Path) -> list[str]:
    """Return what kai reads from a new daemon over the relays the old one used."""
    factory = CountingFactory()
    factory.relay_type = ConsumingRelay
    run_dir = RunDir(tmp_path / "run-restarted")
    restarted = BiffDaemon(
        run_dir=run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=factory,
        request_timeout=0.5,
    )
    await restarted.start()
    try:
        client = await DaemonClient.connect(
            run_dir=run_dir,
            port=restarted.port,
            identity=ClientIdentity(
                client="cli", session_id="s-kai", cwd="/work/repo", user="kai"
            ),
        )
        try:
            return sorted(_bodies(await client.request("read")))
        finally:
            await client.close()
    finally:
        await restarted.stop()


async def test_mail_a_read_took_while_the_daemon_stopped_is_there_after_a_restart(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(FetchingRelay, "consumed", asyncio.Event())
    harness.factory.relay_type = FetchingRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="do not lose me")
    reading = asyncio.create_task(kai.request("read"))
    await asyncio.wait_for(FetchingRelay.consumed.wait(), _WAIT)

    await harness.daemon.stop()  # the relay has given the mail up; nothing replied yet

    await asyncio.gather(reading, return_exceptions=True)
    assert await _read_after_restart(harness, tmp_path) == ["do not lose me"]


async def test_one_held_message_the_relay_refuses_does_not_drop_the_others(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    harness.factory.relay_type = FlakyRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for body in ("m1", "m2", "m3"):
        await eric.request("write", to="kai", message=body)
    await kai.request_held("read")  # all three are held, none confirmed
    monkeypatch.setattr(FlakyRelay, "failing", 1)  # the first publish at stop fails

    await harness.daemon.stop()

    assert await _read_after_restart(harness, tmp_path) == ["m2", "m3"]


async def test_a_read_that_never_finishes_does_not_hold_the_stop_or_the_held_mail(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(StuckRelay, "stuck", asyncio.Event())
    monkeypatch.setattr(StuckRelay, "release", asyncio.Event())
    monkeypatch.setattr(Handback, "SETTLE_SECONDS", 0.1)
    harness.factory.relay_type = StuckRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to="kai", message="m1")
    await kai.request_held("read")  # m1 is held, unconfirmed
    await eric.request("write", to="kai", message="m2")
    monkeypatch.setattr(StuckRelay, "hanging", True)
    reading = asyncio.create_task(kai.request("read"))
    await asyncio.wait_for(StuckRelay.stuck.wait(), _WAIT)  # hangs before taking m2

    try:
        async with asyncio.timeout(0.4):  # the daemon's own wait is 0.5 s
            await harness.daemon.stop()
        # The stopped daemon's process is gone, so its read takes nothing more:
        # m1 was held and is filed back; m2 was never taken and is still there.
        assert await _read_after_restart(harness, tmp_path) == ["m1", "m2"]
    finally:
        StuckRelay.release.set()  # end the leftover read so the test leaves none
        await asyncio.gather(reading, return_exceptions=True)


async def test_session_mail_a_read_took_before_a_stuck_broadcast_fetch_survives_a_stop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(BroadcastStuckRelay, "stuck", asyncio.Event())
    monkeypatch.setattr(BroadcastStuckRelay, "release", asyncio.Event())
    monkeypatch.setattr(Handback, "SETTLE_SECONDS", 0.1)
    harness.factory.relay_type = BroadcastStuckRelay
    kai = await harness.connect("s-kai", "kai")
    tty = cast("dict[str, dict[str, str]]", await kai.request("status"))["data"][
        "tty_name"
    ]
    eric = await harness.connect("s-eric", "eric")
    await eric.request("write", to=f"kai:{tty}", message="for this session")
    reading = asyncio.create_task(kai.request("read"))
    # The session fetch took the mail; the broadcast fetch has not returned.
    await asyncio.wait_for(BroadcastStuckRelay.stuck.wait(), _WAIT)
    try:
        await harness.daemon.stop()  # the wait for the read runs out
        assert await _read_after_restart(harness, tmp_path) == ["for this session"]
    finally:
        BroadcastStuckRelay.release.set()
        await asyncio.gather(reading, return_exceptions=True)


async def test_slow_publishes_at_stop_end_at_a_total_budget_and_are_logged(
    harness: Harness,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(Handback, "REFILE_SECONDS", 0.1)
    harness.factory.relay_type = StalledPublishRelay
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    for body in ("m1", "m2", "m3"):
        await eric.request("write", to="kai", message=body)
    await kai.request_held("read")  # all three are held, none confirmed
    monkeypatch.setattr(StalledPublishRelay, "stalled", True)

    async with asyncio.timeout(0.4):  # the first publish would never end
        await harness.daemon.stop()

    assert "3 held messages were not given back" in caplog.text
