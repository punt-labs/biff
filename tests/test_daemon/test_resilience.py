"""The daemon survives relay outages and bugs, and tells the client which is which."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

import pytest

from biff import commands
from biff.client.connection import (
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)
from biff.commands import CommandResult
from biff.frame_reply import ErrorCode
from biff.models import UnreadSummary, UserSession
from biff.relay import LocalRelay
from biff.session_id import CLI_ROLE
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_WAIT = 5.0


class FlakyRelay(LocalRelay):
    """A local relay a test can break: an outage, a bug, or slowness."""

    def __init__(self, data_dir: Path) -> None:
        super().__init__(data_dir)
        self.outage = False
        self.bug = False
        self.heartbeat_bug = False
        self.slow_session = 0.0
        self.slow_who = 0.0
        self.closed = False

    def _check(self) -> None:
        if self.outage:
            msg = "relay down"
            raise OSError(msg)
        if self.bug:
            msg = "bug in the relay"
            raise RuntimeError(msg)

    async def inbox_high_water(self, session_key: str) -> int:
        self._check()
        return await super().inbox_high_water(session_key)

    async def get_unread_summary(self, session_key: str) -> UnreadSummary:
        self._check()
        return await super().get_unread_summary(session_key)

    async def heartbeat(self, session_key: str) -> None:
        if self.heartbeat_bug:
            msg = "heartbeat bug"
            raise RuntimeError(msg)
        await super().heartbeat(session_key)

    async def get_session(self, session_key: str) -> UserSession | None:
        await asyncio.sleep(self.slow_session)
        return await super().get_session(session_key)

    async def get_sessions_for_repos(self, repos: frozenset[str]) -> list[UserSession]:
        await asyncio.sleep(self.slow_who)
        return await super().get_sessions_for_repos(repos)

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def relays(harness: Harness) -> list[FlakyRelay]:
    made: list[FlakyRelay] = []

    def build(directory: Path) -> FlakyRelay:
        relay = FlakyRelay(directory)
        made.append(relay)
        return relay

    harness.factory.relay_type = build
    return made


async def _events(client: DaemonClient, count: int) -> list[str]:
    return [
        (await asyncio.wait_for(client.next_event(), _WAIT)).kind for _ in range(count)
    ]


async def test_a_baseline_outage_is_retried_not_fatal(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    relays[0].outage = True
    await kai.subscribe()
    await asyncio.sleep(0.2)
    relays[0].outage = False
    assert await _events(kai, 3) == ["degraded", "recovered", "unread"]


async def test_an_outage_after_the_baseline_is_a_degraded_then_recovered_pair(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    await kai.subscribe()
    assert await _events(kai, 1) == ["unread"]
    relays[0].outage = True
    assert await _events(kai, 1) == ["degraded"]
    relays[0].outage = False
    assert await _events(kai, 1) == ["recovered"]


async def test_one_outage_is_one_degraded_event_not_one_per_poll(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    await kai.subscribe()
    await _events(kai, 1)
    relays[0].outage = True
    await asyncio.sleep(0.3)  # fifteen polls
    relays[0].outage = False
    assert await _events(kai, 2) == ["degraded", "recovered"]


async def test_an_outage_is_logged_once_not_every_poll(
    harness: Harness, relays: list[FlakyRelay], caplog: pytest.LogCaptureFixture
) -> None:
    kai = await harness.connect("s-kai", "kai")
    await kai.subscribe()
    await _events(kai, 1)
    with caplog.at_level(logging.INFO, logger="biff.daemon.subscription"):
        relays[0].outage = True
        await asyncio.sleep(0.3)
        relays[0].outage = False
        await _events(kai, 2)
    loud = [r for r in caplog.records if r.levelno >= logging.INFO]
    assert 1 <= len(loud) <= 2


async def test_a_bug_in_the_subscription_closes_the_connection_and_logs_an_error(
    harness: Harness, relays: list[FlakyRelay], caplog: pytest.LogCaptureFixture
) -> None:
    kai = await harness.connect("s-kai", "kai")
    relays[0].bug = True
    with caplog.at_level(logging.ERROR):
        await kai.subscribe()
        with pytest.raises(ConnectionLostError, match=r"1011"):
            await asyncio.wait_for(kai.next_event(), _WAIT)
    assert any(r.levelno >= logging.ERROR for r in caplog.records)


async def test_a_dead_heartbeat_task_is_logged_at_error(
    harness: Harness, relays: list[FlakyRelay], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.ERROR):
        await harness.connect("s-kai", "kai")
        relays[0].heartbeat_bug = True
        await asyncio.sleep(0.3)
    assert any("heartbeat" in r.getMessage() for r in caplog.records)


async def test_a_replaced_run_cannot_re_subscribe(harness: Harness) -> None:
    old = await harness.connect("s-kai", "kai", main_agent_id="run-1")
    eric = await harness.connect("s-eric", "eric")
    new = await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises((DaemonError, ConnectionLostError)):
        await old.subscribe()
        await asyncio.wait_for(old.next_event(), _WAIT)
    await new.subscribe()
    await asyncio.wait_for(new.next_event(), _WAIT)
    await eric.request("write", to="kai", message="x")
    assert old.pending_events == 0


async def test_a_replaced_run_is_closed_with_a_named_reason(harness: Harness) -> None:
    old = await harness.connect("s-kai", "kai", main_agent_id="run-1")
    await harness.connect("s-kai", "kai", main_agent_id="run-2")
    with pytest.raises(ConnectionLostError, match=r"1008.*replaced"):
        await asyncio.wait_for(old.next_event(), _WAIT)


async def test_a_second_subscribe_is_an_error_reply_not_a_silent_cancel(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    await kai.subscribe()
    with pytest.raises(DaemonError) as caught:
        await kai.subscribe()
    assert caught.value.code is ErrorCode.ALREADY_SUBSCRIBED


async def test_the_first_subscription_survives_a_refused_second(
    harness: Harness,
) -> None:
    kai = await harness.connect("s-kai", "kai")
    eric = await harness.connect("s-eric", "eric")
    await kai.subscribe()
    await asyncio.wait_for(kai.next_event(), _WAIT)
    with pytest.raises(DaemonError):
        await kai.subscribe()
    await eric.request("write", to="kai", message="still here")
    assert (await asyncio.wait_for(kai.next_event(), _WAIT)).kind == "mail"


async def test_reattaching_recreates_a_vanished_relay_row(harness: Harness) -> None:
    first = await harness.connect("s-kai", "kai")
    await first.close()
    await asyncio.sleep(0.1)
    relay = LocalRelay(harness.factory.built[0])
    key = SessionRouting("s-kai", role=CLI_ROLE).session_key("kai")
    await relay.delete_session(key)
    again = await harness.connect("s-kai", "kai")
    assert await relay.get_session(key) is not None
    assert cast("dict[str, object]", await again.request("who"))["error"] is False


async def test_a_hello_that_is_refused_opens_no_relay(harness: Harness) -> None:
    await harness.connect("s-kai", "kai", cwd="/work/repo-a")
    with pytest.raises(DaemonError):
        await harness.connect("s-kai", "kai", cwd="/work/repo-b")
    assert len(harness.factory.built) == 1
    assert harness.daemon.status().relay_connections == 1


async def test_a_failed_hello_leaves_no_lock_behind(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    class Down(FlakyRelay):
        async def get_session(self, session_key: str) -> UserSession | None:
            msg = "relay down"
            raise OSError(msg)

    harness.factory.relay_type = Down
    with pytest.raises(DaemonError):
        await harness.connect("s-kai", "kai")
    assert harness.daemon._core.sessions._locks == {}


async def test_a_relay_that_never_answers_the_hello_is_a_named_timeout(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    class Slow(FlakyRelay):
        def __init__(self, data_dir: Path) -> None:
            super().__init__(data_dir)
            self.slow_session = 30.0

    harness.factory.relay_type = Slow
    with pytest.raises(DaemonError, match=r"relay did not answer within") as caught:
        await harness.connect("s-kai", "kai")
    assert caught.value.code is ErrorCode.INTERNAL


async def test_a_slow_request_does_not_block_the_next_one(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    relays[0].slow_who = 0.3
    slow = asyncio.create_task(kai.request("who"))
    await asyncio.sleep(0.05)
    status = cast("dict[str, object]", await kai.request("status"))
    assert (status["error"], slow.done()) == (False, False)
    await slow


async def test_a_request_the_relay_never_finishes_is_an_internal_error(
    harness: Harness, relays: list[FlakyRelay]
) -> None:
    kai = await harness.connect("s-kai", "kai")
    relays[0].slow_who = 30.0
    with pytest.raises(DaemonError, match=r"who.*timed out") as caught:
        await kai.request("who")
    assert caught.value.code is ErrorCode.INTERNAL


async def test_an_unexpected_exception_is_an_internal_reply_and_the_connection_lives(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(_ctx: object) -> CommandResult:
        return CommandResult(text="", json_data=1 // 0)

    kai = await harness.connect("s-kai", "kai")
    monkeypatch.setattr(commands, "who", broken)
    with pytest.raises(DaemonError, match=r"who failed in biffd.*biff\.log") as caught:
        await kai.request("who")
    assert caught.value.code is ErrorCode.INTERNAL
    monkeypatch.undo()
    assert cast("dict[str, object]", await kai.request("who"))["error"] is False


async def test_an_unexpected_exception_is_logged_with_its_traceback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    async def broken(_ctx: object) -> CommandResult:
        return CommandResult(text="", json_data=1 // 0)

    kai = await harness.connect("s-kai", "kai")
    monkeypatch.setattr(commands, "who", broken)
    with caplog.at_level(logging.ERROR), pytest.raises(DaemonError):
        await kai.request("who")
    assert any(r.exc_info for r in caplog.records)


async def test_an_unserialisable_payload_fails_loudly_not_as_a_string(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def odd(_ctx: object) -> CommandResult:
        return CommandResult(text="", json_data={"bad": object()})

    kai = await harness.connect("s-kai", "kai")
    monkeypatch.setattr(commands, "who", odd)
    with pytest.raises(DaemonError) as caught:
        await kai.request("who")
    assert caught.value.code is ErrorCode.INTERNAL


async def test_a_resolver_bug_is_an_internal_hello_refusal_not_a_dead_socket(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(_hello: object) -> object:
        msg = "resolver bug"
        raise RuntimeError(msg)

    monkeypatch.setattr(harness.daemon._core.resolver, "resolve", boom)
    with pytest.raises(DaemonError, match=r"biff\.log") as caught:
        await harness.connect("s-kai", "kai")
    assert caught.value.code is ErrorCode.INTERNAL


async def test_stop_cleans_up_even_when_the_listener_fails_to_close(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    class BrokenServer:
        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            msg = "listener would not close"
            raise RuntimeError(msg)

    await harness.connect("s-kai", "kai")
    real = harness.daemon._server
    assert real is not None
    monkeypatch.setattr(harness.daemon, "_server", BrokenServer())
    with pytest.raises(RuntimeError, match="would not close"):
        await harness.daemon.stop()
    assert not harness.run_dir.port_path.exists()
    assert harness.daemon.status().relay_connections == 0
    real.close()


async def test_stop_leaves_a_port_file_another_daemon_wrote(harness: Harness) -> None:
    harness.run_dir.write_port(harness.daemon.port + 1)
    await harness.daemon.stop()
    assert harness.run_dir.read_port() == harness.daemon.port + 1
