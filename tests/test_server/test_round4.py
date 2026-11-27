"""Round-4: the degraded marker is state, a death is judged by liveness passes."""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import uuid
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage

import biff.local_watch as local_watch
from biff.abandoned_canaries import AbandonedCanaries
from biff.local_watch import LocalWatch, WatchKeeper
from biff.models import BiffConfig, Message
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server import app, channel, poller_supervisor
from biff.server.app import create_server
from biff.server.channel import ChannelLink, ChannelUnavailableError
from biff.server.poller_supervisor import PollerSupervisor
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

_MARK = "[biff: notifications"
_REAL_POLL = _descriptions.poll_inbox


def _state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name="_round4"),
        tmp_path / "relay",
        tty="tty1",
        hostname="h",
        pwd="/p",
        unread_path=tmp_path / "status" / "unread.json",
    )


async def _until(check: Callable[[], bool], what: str, limit: float = 15.0) -> None:
    try:
        async with asyncio.timeout(limit):
            while not check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"never happened: {what}")


def _marked(description: str | None) -> bool:
    return (description or "").startswith(_MARK)


async def _dying(*_a: object, **_k: object) -> None:
    msg = "deterministic bug"
    raise RuntimeError(msg)


@pytest.fixture(autouse=True)
def _fast_restarts(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(poller_supervisor, "_RESTART_DELAY", 0.01)
    monkeypatch.setattr(poller_supervisor, "_RESTART_CAP", 0.05)
    monkeypatch.setattr(poller_supervisor, "_DEGRADED_AFTER", 3)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    _descriptions._reset_session()


class TestTheMarkerIsState:
    async def test_a_refresh_does_not_erase_it_while_the_poller_is_dead(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_descriptions, "poll_inbox", _dying)
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        PollerSupervisor(live, mcp, state).start()
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        await _until(lambda: _marked(tool.description), "the marker is shown")
        await _descriptions.refresh_read_messages(mcp, state)  # a /who-style refresh
        assert _marked(tool.description)
        live.shutdown.set()

    async def test_every_death_at_or_past_the_threshold_keeps_it_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_descriptions, "poll_inbox", _dying)
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        PollerSupervisor(live, mcp, state).start()
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        await _until(lambda: _marked(tool.description), "the marker is shown")
        for _ in range(15):
            await asyncio.sleep(0.05)
            await _descriptions.refresh_read_messages(mcp, state)
            assert _marked(tool.description)
        live.shutdown.set()

    async def test_a_full_pass_with_a_liveness_check_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def flaky(*a: object, **k: object) -> object:
            calls.append(1)
            if len(calls) <= 3:
                return _dying()
            return _REAL_POLL(*a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(_descriptions, "poll_inbox", flaky)
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 0.3)
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        PollerSupervisor(live, mcp, state).start()
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        await _until(lambda: _marked(tool.description), "the marker is shown")
        await _until(lambda: not _marked(tool.description), "the marker clears")
        live.shutdown.set()
        await asyncio.sleep(0.1)

    async def test_a_restarted_poller_that_has_not_yet_checked_liveness_keeps_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[int] = []

        def flaky(*a: object, **k: object) -> object:
            calls.append(1)
            if len(calls) <= 3:
                return _dying()
            return _REAL_POLL(*a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(_descriptions, "poll_inbox", flaky)
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 3600.0)
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        PollerSupervisor(live, mcp, state).start()
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        await _until(lambda: _marked(tool.description), "the marker is shown")
        await _until(lambda: len(calls) > 3, "the poller is restarted")
        await asyncio.sleep(0.5)  # it has completed its first pass, not a check
        assert _marked(tool.description)
        live.shutdown.set()
        await asyncio.sleep(0.1)


class TestADeathIsJudgedByLivenessPasses:
    async def test_dying_at_each_liveness_pass_still_counts_in_a_row(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def lives_long_then_dies(*_a: object, **_k: object) -> None:
            await asyncio.sleep(0.12)  # longer than the cap, never a check
            msg = "dies at its liveness pass"
            raise RuntimeError(msg)

        monkeypatch.setattr(_descriptions, "poll_inbox", lives_long_then_dies)
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        PollerSupervisor(live, mcp, state).start()
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        await _until(lambda: _marked(tool.description), "the marker is shown")
        live.shutdown.set()


class TestARestartThatRaisesIsRetried:
    async def test_it_shows_the_marker_and_tries_again(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        calls: list[int] = []

        def poll(*a: object, **k: object) -> object:
            calls.append(1)
            if len(calls) == 1:
                return _dying()
            if len(calls) == 2:
                msg = "cannot build the poller"
                raise RuntimeError(msg)
            return _REAL_POLL(*a, **k)  # type: ignore[arg-type]

        monkeypatch.setattr(_descriptions, "poll_inbox", poll)
        monkeypatch.setattr(
            poller_supervisor, "_DEGRADED_AFTER", 100
        )  # only this branch
        state = _state(tmp_path)
        mcp = create_server(state)
        live = app._LiveSession()
        with caplog.at_level(logging.ERROR, logger="biff.server.poller_supervisor"):
            PollerSupervisor(live, mcp, state).start()
            tool = await mcp.get_tool("read_messages")
            assert tool is not None
            await _until(lambda: _marked(tool.description), "the marker is shown")
            await _until(lambda: len(calls) >= 3, "the spawn is retried")
        live.shutdown.set()
        await asyncio.sleep(0.1)


class _BrokenWatch(LocalWatch):
    """A watch whose health check has a bug."""

    @property
    def is_started(self) -> bool:
        return True

    def unhealthy_reason(self, canary_timeout: float | None = None) -> str | None:
        msg = "bug in the check"
        raise TypeError(msg)


class _CountingKeeper(WatchKeeper):
    """A keeper that counts rebuilds instead of starting a watch."""

    built = 0

    def _build(self, watch: LocalWatch) -> None:
        type(self).built += 1


class TestAHealthCheckBugDoesNotKillThePoller:
    async def test_a_check_that_raises_is_logged_and_rebuilds_the_watch(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        shared = tmp_path / "relay"
        shared.mkdir()
        keeper = _CountingKeeper((_BrokenWatch(shared, lambda: None),))
        with caplog.at_level(logging.ERROR, logger=local_watch.logger.name):
            assert keeper.check() is True
        assert _CountingKeeper.built == 1
        assert any("bug in the check" in r.getMessage() for r in caplog.records)


class TestAnOwedDebtWakesTheQuietPoller:
    @staticmethod
    def _link(monkeypatch: pytest.MonkeyPatch, backoff: float) -> ChannelLink:
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        _descriptions.handshake_delivery.serve_single_connection(link)
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", backoff)
        return link

    def test_nothing_is_armed_when_nothing_is_owed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._link(monkeypatch, 0.2)
        assert _descriptions.handshake_delivery.seconds_until_retry() is None

    async def test_the_debt_is_retried_when_the_backoff_expires(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = self._link(monkeypatch, 0.4)
        delivery = _descriptions.handshake_delivery
        session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        session.send_tool_list_changed = AsyncMock()
        await delivery.capture_session(session)
        state = _state(tmp_path)
        task = asyncio.create_task(_REAL_POLL(create_server(state), state))
        await asyncio.sleep(0.3)  # the first pass is over; the relay is quiet
        link.failed("send", TimeoutError())  # a send failed: backoff starts
        delivery._pending_notify = True
        state.wake.wake()  # as ``_announce`` does when a tool call leaves a debt
        started = asyncio.get_running_loop().time()
        await _until(lambda: not delivery._pending_notify, "the debt is retried")
        waited = asyncio.get_running_loop().time() - started
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert 0.2 < waited < 1.0  # at the backoff, not at a 60 s liveness check


class TestAnnouncingADebtWakesThePoller:
    async def test_a_tool_call_that_leaves_a_debt_wakes_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(link)
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 30.0)
        link.failed("send", TimeoutError())
        delivery._pending_notify = True
        state = _state(tmp_path)
        await state.wake.wait(timeout=0.0, shutdown=None)  # drain
        await _descriptions._announce(state)
        outcome = await state.wake.wait(timeout=0.5, shutdown=None)
        assert outcome.is_arrival

    async def test_nothing_owed_wakes_nobody(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        await state.wake.wait(timeout=0.0, shutdown=None)  # drain
        await _descriptions._announce(state)
        outcome = await state.wake.wait(timeout=0.2, shutdown=None)
        assert not outcome.is_arrival


class TestADebtInsideTheHandshakeWindowWakesOnce:
    async def test_one_wake_at_the_window_end_and_no_ticks(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(channel, "HANDSHAKE_WINDOW", 2.5)
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(link)
        delivery._pending_notify = True  # owed, and no session was captured
        loop = asyncio.get_running_loop()
        started = loop.time()
        flushes: list[float] = []
        real_flush = type(delivery).flush_owed

        async def spy(self: object) -> None:
            flushes.append(loop.time() - started)
            await real_flush(delivery)

        monkeypatch.setattr(type(delivery), "flush_owed", spy)
        state = _state(tmp_path)
        task = asyncio.create_task(_REAL_POLL(create_server(state), state))
        await asyncio.sleep(3.2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        early = [t for t in flushes if 0.3 < t < 2.3]
        at_end = [t for t in flushes if 2.3 <= t < 2.9]
        assert early == []  # no recheck inside the window
        assert len(at_end) == 1  # one wake, when the window ends


@pytest.fixture
def only_this_process_is_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the liveness probe report every pid but this process's as gone.

    The reclaim tests name probe directories for pids they need to be dead.  A real
    pid cannot be guaranteed dead: the OS may hand a reaped child's pid, or a
    literal such as 4194303, to a live process before the probe runs.  Replacing
    the probe makes the tests independent of the host's pid space.
    """
    me = os.getpid()

    def is_running(pid: int) -> bool:
        return pid == me

    monkeypatch.setattr(AbandonedCanaries, "_is_running", staticmethod(is_running))


class TestAbandonedProbeDirectories:
    @pytest.mark.usefixtures("only_this_process_is_alive")
    async def test_a_dead_sessions_probe_is_reclaimed_and_a_live_ones_is_not(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "canaries"
        monkeypatch.setattr(LocalWatch, "CANARY_ROOT", root)
        dead = root / "4194303-abc"
        live = root / f"{os.getpid()}-other"
        for d in (dead, live):
            d.mkdir(parents=True)
        root.chmod(0o700)
        with LocalWatch(tmp_path, lambda: None):
            pass
        assert not dead.exists()
        assert live.exists()


class TestReadingSharedMailPokesTheSiblings:
    async def test_marking_a_broadcast_read_pokes_the_users_other_sessions(
        self,
    ) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        poke = AsyncMock()
        relay._publish_inbox_notification = poke  # type: ignore[method-assign]
        await relay.mark_read_user_inbox("kai", [uuid.uuid4()])
        poke.assert_awaited_once_with("test", "kai")


class _CountingIds(list["uuid.UUID"]):
    """A sequence of ids that counts how often it is iterated."""

    iterations = 0

    def __iter__(self) -> Iterator[uuid.UUID]:
        type(self).iterations += 1
        return super().__iter__()


class TestMarkingReadBuildsTheIdSetOnce:
    async def test_the_ids_are_iterated_once_for_the_whole_mailbox(
        self, tmp_path: Path
    ) -> None:
        relay = LocalRelay(tmp_path)
        messages = [
            Message(from_user="eric", to_user="kai", body=str(i)) for i in range(40)
        ]
        for message in messages:
            await relay.deliver(message)
        ids = _CountingIds(m.id for m in messages[:5])
        await relay.mark_read_user_inbox("kai", ids)
        assert _CountingIds.iterations == 1


class TestAFailedPassIsNotARecovery:
    async def test_the_notice_stays_while_every_pass_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def failing(
            _mcp: object, _state: object, seen: _descriptions._Seen, **_k: object
        ) -> _descriptions._Seen:
            return _descriptions._Seen(failed=True, wall_expires=seen.wall_expires)

        monkeypatch.setattr(_descriptions, "_safe_tick", failing)
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 0.2)
        state = _state(tmp_path)
        state.wake.health.degrade()
        task = asyncio.create_task(_REAL_POLL(create_server(state), state))
        await asyncio.sleep(1.0)  # several liveness passes, all of them failed
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert state.wake.health.is_degraded


class TestACancelledSendStillWakesThePoller:
    async def test_the_debt_it_leaves_arms_the_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = ChannelLink()
        link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
        delivery = _descriptions.handshake_delivery
        delivery.serve_single_connection(link)
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 30.0)
        link.failed("send", TimeoutError())
        state = _state(tmp_path)

        async def owes_then_hangs(_bus: object = None) -> None:
            delivery._pending_notify = True  # the debt is owed before the await
            await asyncio.Event().wait()

        monkeypatch.setattr(delivery, "notify", owes_then_hangs)
        await state.wake.wait(timeout=0.0, shutdown=None)  # drain
        task = asyncio.create_task(_descriptions._announce(state))
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        outcome = await state.wake.wait(timeout=0.2, shutdown=None)
        assert outcome.is_arrival


def _owing_link(
    monkeypatch: pytest.MonkeyPatch, *, window: float = 0.0, backoff: float = 0.0
) -> ChannelLink:
    monkeypatch.setattr(channel, "HANDSHAKE_WINDOW", window)
    monkeypatch.setattr(channel, "CHANNEL_BACKOFF", backoff)
    link = ChannelLink()
    link.observe(anyio.create_memory_object_stream[SessionMessage](1)[0])
    _descriptions.handshake_delivery.serve_single_connection(link)
    return link


class TestOnlyAPlausibleRetryIsArmed:
    def test_no_session_after_the_window_arms_nothing(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        link = _owing_link(monkeypatch, window=0.0, backoff=5.0)
        link.failed("send", ChannelUnavailableError("no session"))  # a back-off runs
        delivery = _descriptions.handshake_delivery
        delivery._pending_notify = True
        assert delivery.seconds_until_retry() is None

    def test_a_closed_link_arms_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        link = _owing_link(monkeypatch, backoff=5.0)
        delivery = _descriptions.handshake_delivery
        delivery._pending_notify = True
        delivery._session = MagicMock(spec=ServerSession)
        link.failed("send", TimeoutError())
        assert delivery.seconds_until_retry() is not None  # open: retried
        link.detach()
        assert delivery.seconds_until_retry() is None  # the connection is gone

    async def test_three_timeouts_in_a_row_stop_the_arming(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        _owing_link(monkeypatch, backoff=0.0)
        delivery = _descriptions.handshake_delivery
        session = MagicMock(spec=ServerSession, protocol_version="2025-11-25")
        session.send_tool_list_changed = AsyncMock(side_effect=TimeoutError)
        await delivery.capture_session(session)
        delivery._pending_notify = True
        monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 5.0)
        armed: list[bool] = []
        for _ in range(4):
            monkeypatch.setattr(channel, "CHANNEL_BACKOFF", 0.0)
            with caplog.at_level(logging.INFO):
                await delivery.flush_owed()
            armed.append(delivery.seconds_until_retry() is not None)
        assert armed == [True, True, False, False]
        assert any("gave up" in r.getMessage() for r in caplog.records)

    def test_a_debt_sendable_now_is_zero_not_none(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _owing_link(monkeypatch, backoff=5.0)
        delivery = _descriptions.handshake_delivery
        delivery._pending_notify = True
        delivery._session = MagicMock(spec=ServerSession)
        assert delivery.seconds_until_retry() == 0.0


class TestACancelledSendWithAReadyLinkStillWakes:
    async def test_a_debt_sendable_now_wakes_the_poller(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _owing_link(monkeypatch, backoff=30.0)
        delivery = _descriptions.handshake_delivery
        delivery._session = MagicMock(spec=ServerSession)
        state = _state(tmp_path)

        async def owes_then_hangs(_bus: object = None) -> None:
            delivery._pending_notify = True
            await asyncio.Event().wait()

        monkeypatch.setattr(delivery, "notify", owes_then_hangs)
        await state.wake.wait(timeout=0.0, shutdown=None)  # drain
        task = asyncio.create_task(_descriptions._announce(state))
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        outcome = await state.wake.wait(timeout=0.2, shutdown=None)
        assert outcome.is_arrival


class TestTheStatusFileIsNotRewrittenWhenUnchanged:
    def test_an_identical_write_leaves_the_file_alone(self, tmp_path: Path) -> None:
        from biff.models import UnreadSummary
        from biff.plugins.claude_code.unread_file import write_unread_file

        path = tmp_path / "unread.json"
        summary = UnreadSummary(count=0)
        write_unread_file(
            path, summary, repo_name="r", user="kai", tty_name="t", biff_enabled=True
        )
        first = path.stat().st_mtime_ns
        write_unread_file(
            path, summary, repo_name="r", user="kai", tty_name="t", biff_enabled=True
        )
        assert path.stat().st_mtime_ns == first


class TestAnotherSessionsWatchWakesNobody:
    async def test_start_checks_and_close_of_one_watch_wake_the_other_zero_times(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            LocalWatch, "CANARY_ROOT", tmp_path / "canaries", raising=False
        )
        shared = tmp_path / "relay"
        shared.mkdir()
        wakes: list[int] = []
        with LocalWatch(shared, lambda: wakes.append(1)) as watch_b:
            assert watch_b.unhealthy_reason(canary_timeout=15.0) is None
            await asyncio.sleep(1.0)  # B's own start and check settle
            wakes.clear()
            with LocalWatch(shared, lambda: None) as watch_a:
                for _ in range(3):
                    assert watch_a.unhealthy_reason(canary_timeout=15.0) is None
            await asyncio.sleep(1.5)
        assert wakes == []


class TestAFailedInboxPokeIsLoggedPerEpisode:
    async def test_onset_then_quiet_then_recovery(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = NatsRelay(url="tls://fake:4222", repo_name="test")
        down = [True]

        async def live_client() -> MagicMock:
            if down[0]:
                msg = "nats unreachable"
                raise ConnectionError(msg)
            return MagicMock(publish=AsyncMock())

        relay._live_nc_or_reconnect = live_client  # type: ignore[method-assign]
        with caplog.at_level(logging.INFO, logger="biff.nats_relay"):
            for _ in range(3):
                await relay.mark_read_user_inbox("kai", [uuid.uuid4()])
            down[0] = False
            await relay.mark_read_user_inbox("kai", [uuid.uuid4()])
        loud = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert [
            ("failed" in r.getMessage(), "recovered" in r.getMessage()) for r in loud
        ] == [
            (True, False),
            (False, True),
        ]


class TestTheProbeRootIsPrivate:
    @staticmethod
    def _victim(tmp_path: Path) -> Path:
        victim = tmp_path / "victim"
        (victim / "4194303-abc").mkdir(parents=True)  # looks like a dead probe
        return victim

    async def test_a_planted_symlink_root_is_refused_and_nothing_is_deleted(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        victim = self._victim(tmp_path)
        root = tmp_path / "root"
        root.symlink_to(victim)
        monkeypatch.setattr(LocalWatch, "CANARY_ROOT", root)
        with (
            caplog.at_level(logging.WARNING, logger="biff.local_watch"),
            LocalWatch(tmp_path, lambda: None) as watch,
        ):
            assert watch.unhealthy_reason(canary_timeout=15.0) is None
        assert (victim / "4194303-abc").exists()
        assert [p.name for p in victim.iterdir()] == ["4194303-abc"]
        assert any("probe directory" in r.getMessage() for r in caplog.records)

    async def test_a_root_open_to_others_is_refused(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = self._victim(tmp_path)
        root.chmod(0o777)
        monkeypatch.setattr(LocalWatch, "CANARY_ROOT", root)
        with (
            caplog.at_level(logging.WARNING, logger="biff.local_watch"),
            LocalWatch(tmp_path, lambda: None),
        ):
            pass
        assert (root / "4194303-abc").exists()
        assert any("probe directory" in r.getMessage() for r in caplog.records)

    async def test_a_symlink_entry_in_a_good_root_is_not_followed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outside = tmp_path / "outside"
        (outside / "keep").mkdir(parents=True)
        root = tmp_path / "root"
        root.mkdir(mode=0o700)
        (root / "4194303-abc").symlink_to(outside)
        monkeypatch.setattr(LocalWatch, "CANARY_ROOT", root)
        with LocalWatch(tmp_path, lambda: None):
            pass
        assert (outside / "keep").exists()

    async def test_a_private_root_is_created_with_mode_0700(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = tmp_path / "state" / "watch-canaries"
        monkeypatch.setattr(LocalWatch, "CANARY_ROOT", root)
        with LocalWatch(tmp_path, lambda: None):
            assert (root.stat().st_mode & 0o777) == 0o700


class TestStoppingANeverStartedWatchTouchesNothing:
    async def test_the_watched_directory_survives_a_stop_before_a_start(
        self, tmp_path: Path
    ) -> None:
        keep = tmp_path / "userinbox-kai.jsonl"
        keep.write_text("x")
        LocalWatch(tmp_path, lambda: None).stop()
        assert keep.exists()


class TestTheRealLivenessProbe:
    def test_an_exited_process_is_reported_gone(self) -> None:
        """The probe says a reaped child's pid is not running.

        This asserts only the probe's documented behavior, so it is not tied to the
        reclaim tests: those replace the probe with a fixed answer, because the OS
        may reassign a reaped pid before any probe can run.
        """
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        assert AbandonedCanaries._is_running(child.pid) is False

    def test_this_process_is_reported_running(self) -> None:
        assert AbandonedCanaries._is_running(os.getpid()) is True


class TestReclaimSurvivesAnotherSessionsCleanup:
    @pytest.mark.usefixtures("only_this_process_is_alive")
    def test_an_entry_gone_between_listing_and_lstat_is_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        root = tmp_path / "canaries"
        root.mkdir(mode=0o700)
        dead = root / "4194303-abc"
        dead.mkdir()
        gone = root / "4194302-def"  # listed, then removed by another session
        real_iterdir = type(root).iterdir

        def listing(self: Path) -> Iterator[Path]:
            yield gone
            yield from real_iterdir(self)

        monkeypatch.setattr(type(root), "iterdir", listing)
        AbandonedCanaries.reclaim(root)
        assert not dead.exists()  # the rest was still reclaimed

    @pytest.mark.usefixtures("only_this_process_is_alive")
    def test_another_os_error_is_logged_and_skipped(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        root = tmp_path / "canaries"
        root.mkdir(mode=0o700)
        dead = root / "4194303-abc"
        dead.mkdir()
        odd = root / "4194302-def"
        odd.mkdir()
        real_lstat = type(root).lstat

        def lstat(self: Path) -> object:
            if self == odd:
                raise PermissionError(13, "denied")
            return real_lstat(self)

        monkeypatch.setattr(type(root), "lstat", lstat)
        with caplog.at_level(logging.INFO, logger="biff.abandoned_canaries"):
            AbandonedCanaries.reclaim(root)
        assert not dead.exists()
        assert odd.exists()
        assert any("denied" in r.getMessage() for r in caplog.records)
