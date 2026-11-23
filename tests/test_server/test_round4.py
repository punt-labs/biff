"""Round-4: the degraded marker is state, a death is judged by liveness passes."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from mcp.server.session import ServerSession
from mcp.shared.message import SessionMessage

import biff.local_watch as local_watch
from biff.local_watch import LocalWatch, WatchKeeper
from biff.models import BiffConfig
from biff.server import app, channel, poller_supervisor
from biff.server.app import create_server
from biff.server.channel import ChannelLink
from biff.server.poller_supervisor import PollerSupervisor
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Callable
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
