"""The daemon's push of an arrival notice wakes the same poll that runs on the clock.

``docs/specs/notification-mail-event.tex``: ``PushWake`` (a push wakes the poll, a
repeat changes nothing, a lost push is covered) and ``PollWake`` (the poll wakes
every kind with or without a push).  The daemon under test polls only every 30
seconds, so an event that arrives within a second arrived by push.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.server import BiffDaemon
from tests.test_daemon.conftest import FakeResolver
from tests.test_daemon.push_relay import PushRelay
from tests.test_daemon.talk_bus import TalkBus

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.frames import Event
    from tests.test_daemon.conftest import Harness

_SOON = 2.0
_QUIET = 0.4


class Rig:
    """A daemon that polls rarely, over relays that push on one shared bus."""

    def __init__(self, harness: Harness, bus: TalkBus, daemon: BiffDaemon) -> None:
        self.harness = harness
        self.bus = bus
        self.daemon = daemon

    async def client(self, session_id: str, user: str) -> DaemonClient:
        client = await DaemonClient.connect(
            run_dir=self.harness.run_dir,
            port=self.daemon.port,
            identity=ClientIdentity(
                client="mcp",
                session_id=session_id,
                main_agent_id="a1",
                cwd="/work/repo",
                user=user,
            ),
        )
        self.harness.clients.append(client)
        return client


@pytest.fixture
async def rig(harness: Harness, tmp_path: Path) -> AsyncIterator[Rig]:
    bus = TalkBus()

    def build(path: Path) -> PushRelay:
        return PushRelay(path, bus)

    harness.factory.relay_type = build
    await harness.daemon.stop()
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=30.0,
        attach_timeout=2.0,
        request_timeout=2.0,
        read_budget=4000,
        org_refresh_interval=0.05,
    )
    await daemon.start()
    yield Rig(harness, bus, daemon)
    await daemon.stop()


async def _next(client: DaemonClient, timeout: float = _SOON) -> Event:
    return await asyncio.wait_for(client.next_event(), timeout=timeout)


async def _subscribed(client: DaemonClient) -> None:
    await client.subscribe()
    assert (await _next(client)).kind == "unread"


async def test_a_push_of_mail_wakes_the_poll_at_once(rig: Rig) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    await _subscribed(kai)
    await eric.request("write", to="kai", message="pushed")
    event = await _next(kai)
    assert event.kind == "mail"


async def test_a_push_of_mail_addressed_to_the_session_wakes_it(rig: Rig) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    status = await kai.request("status")
    tty = status["data"]["tty_name"]  # type: ignore[index,call-overload]
    await _subscribed(kai)
    await eric.request("write", to=f"kai:{tty}", message="to you")
    assert (await _next(kai)).kind == "mail"


async def test_a_push_of_a_wall_wakes_the_poll_at_once(rig: Rig) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    await _subscribed(kai)
    await eric.request("wall", message="freeze", duration="1h")
    assert (await _next(kai)).kind == "wall"


async def test_a_duplicated_push_announces_an_arrival_once(rig: Rig) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    await _subscribed(kai)
    await eric.request("write", to="kai", message="once")
    assert (await _next(kai)).kind == "mail"
    for _ in range(5):  # the transport delivers the same notice again and again
        await rig.bus.publish("notify.test.kai", b"1")
    with pytest.raises(TimeoutError):
        await _next(kai, timeout=_QUIET)


async def test_a_lost_push_is_covered_by_the_poll(
    harness: Harness, tmp_path: Path
) -> None:
    bus = TalkBus()

    def build(path: Path) -> PushRelay:
        return PushRelay(path, bus)

    harness.factory.relay_type = build
    kai = await harness.connect("s-kai", "kai", client="mcp")
    eric = await harness.connect("s-eric", "eric", client="mcp")
    await _subscribed(kai)
    bus.broken = True  # the poke is lost; the mail is not
    await eric.request("write", to="kai", message="found by the poll")
    bus.broken = False
    assert (await _next(kai)).kind == "mail"  # the harness polls every 20 ms


async def test_a_pushed_pass_reads_every_kind(rig: Rig) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    await _subscribed(kai)
    rig.bus.broken = True  # both pokes are lost
    await eric.request("write", to="kai", message="m")
    await eric.request("wall", message="w", duration="1h")
    rig.bus.broken = False
    await rig.bus.publish("notify.test.kai", b"1")  # one poke, for mail
    kinds = {(await _next(kai)).kind, (await _next(kai)).kind}
    assert kinds == {"mail", "wall"}  # the pass the poke woke read the wall too


async def test_mail_for_a_client_that_left_ends_its_subscription_quietly(
    rig: Rig, caplog: pytest.LogCaptureFixture
) -> None:
    kai = await rig.client("s-kai", "kai")
    eric = await rig.client("s-eric", "eric")
    await _subscribed(kai)
    await kai.close()
    with caplog.at_level("WARNING"):
        await eric.request("write", to="kai", message="nobody listens")
        await asyncio.sleep(_QUIET)
    assert "failed in biffd" not in caplog.text
