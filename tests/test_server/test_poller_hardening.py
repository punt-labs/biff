"""Round-2 hardening of the poller, the local watch and the local relay (DES-087).

Real objects: real ``LocalRelay``, real watches and threads, real ``TalkState``.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing as mp
import select
import sys
import threading
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastmcp.client.transports import FastMCPTransport
from watchdog.observers import Observer

import biff.directory_kqueue as dk
import biff.failure_log as failure_log
import biff.local_watch as local_watch
from biff.commands._messaging import deliver_with_retry
from biff.local_watch import LocalWatch, WatchKeeper
from biff.models import BiffConfig, Message, UserSession, WallPost
from biff.relay import LocalRelay
from biff.server import poller_supervisor
from biff.server.app import create_server
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import poll_inbox
from biff.testing.caller import StampedClient, caller_for
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Callable

_DEADLINE = 15.0


def _watch_threads() -> list[str]:
    return [
        t.name
        for t in threading.enumerate()
        if t.name.startswith("biff-watch") or "watchdog" in type(t).__module__
    ]


async def _until(check: Callable[[], bool], what: str) -> None:
    try:
        async with asyncio.timeout(_DEADLINE):
            while not check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"never happened: {what}")


def _state(tmp_path: Path, name: str = "relay") -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name="_hardening"),
        tmp_path / name,
        tty="tty1",
        hostname="h",
        pwd="/p",
        unread_path=tmp_path / "status" / f"{name}.json",
    )


def _deaf(*_a: object) -> None:
    """Swallow an event or a wake, as a stalled watcher would."""


class _DeafKqueue:
    """A kqueue that is alive and never reports anything."""

    is_alive = True

    def __init__(self, *_a: object) -> None:
        """Accept and ignore the path and callback."""

    def stop(self) -> bool:
        """Report that it stopped."""
        return True


class TestCancelledPollerLeavesNoWatch:
    """A re-point cancels the poller while its watches are still starting."""

    @pytest.mark.parametrize("delay", [0.0, 0.005, 0.05, 1.0])
    async def test_cancel_at_any_point_leaves_no_watch_thread(
        self, tmp_path: Path, delay: float
    ) -> None:
        before = len(_watch_threads())
        state = _state(tmp_path)
        task = asyncio.create_task(
            poll_inbox(
                create_server(state), state, watch_dirs=(tmp_path / "sentinels",)
            )
        )
        await asyncio.sleep(delay)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        await _until(lambda: len(_watch_threads()) <= before, "watch threads ended")
        await asyncio.sleep(1.0)  # a late-finishing start would show up here
        assert len(_watch_threads()) <= before

    async def test_a_closed_watch_never_starts(self, tmp_path: Path) -> None:
        watch = LocalWatch(tmp_path, lambda: None)
        watch.close()
        before = len(_watch_threads())
        watch.start()
        assert len(_watch_threads()) == before


_SESSIONS = 150


def _register_sessions(data: str) -> None:
    relay = LocalRelay(Path(data))
    for j in range(_SESSIONS):
        asyncio.run(relay.update_session(UserSession(user="kai", tty=f"t{j:04d}")))


def _reap_until(data: str, stop: str) -> None:
    relay = LocalRelay(Path(data))
    k = 0
    while not Path(stop).exists():
        asyncio.run(relay.update_session(UserSession(user="eric", tty=f"x{k}")))
        relay.write_remove_sentinel(f"eric:x{k}")
        relay.reap_sentinels()
        k += 1


class TestReapDoesNotLoseSessions:
    def test_a_reap_racing_registrations_loses_none(self, tmp_path: Path) -> None:
        data = tmp_path / "relay"
        data.mkdir()
        stop = tmp_path / "stop"
        ctx = mp.get_context("spawn")
        reaper = ctx.Process(target=_reap_until, args=(str(data), str(stop)))
        registrar = ctx.Process(target=_register_sessions, args=(str(data),))
        reaper.start()
        registrar.start()
        registrar.join()
        stop.touch()
        reaper.join()
        kept = {k for k in LocalRelay(data)._read_sessions() if k.startswith("kai:")}
        assert len(kept) == _SESSIONS


class TestLivenessIsNotStarved:
    async def test_steady_wakes_do_not_starve_the_watch_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 0.3)
        checks: list[float] = []
        real = WatchKeeper.check

        def counting(self: WatchKeeper) -> bool:
            checks.append(time.monotonic())
            return real(self)

        monkeypatch.setattr(WatchKeeper, "check", counting)
        state = _state(tmp_path)
        task = asyncio.create_task(poll_inbox(create_server(state), state))
        try:
            end = time.monotonic() + 5.0
            while time.monotonic() < end:  # another session keeps waking us
                state.wake.wake()
                await asyncio.sleep(0.05)
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        assert len(checks) >= 3  # each check also waits on its canary event


class TestWatchFailureReporting:
    async def test_the_real_cause_of_a_failed_start_is_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        real = Observer

        class Failing(real):  # type: ignore[valid-type,misc]
            def start(self) -> None:
                msg = "inotify watch limit reached"
                raise OSError(28, msg)

        monkeypatch.setattr(local_watch, "Observer", Failing)
        with pytest.raises(OSError, match="inotify watch limit"):
            LocalWatch(tmp_path, lambda: None).start()

    @pytest.mark.skipif(sys.platform != "darwin", reason="kqueue is macOS/BSD")
    async def test_failed_kqueue_construction_leaks_no_descriptor(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(*_a: object, **_k: object) -> object:
            msg = "too many open files"
            raise OSError(24, msg)

        monkeypatch.setattr(select, "kqueue", boom)
        before = len(list(Path("/dev/fd").iterdir()))
        for _ in range(10):
            with pytest.raises(OSError, match="too many"):
                dk.DirectoryKqueue(tmp_path, lambda: None)
        assert len(list(Path("/dev/fd").iterdir())) == before

    async def test_a_stalled_watcher_fails_the_canary_and_names_the_check(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        with LocalWatch(tmp_path, lambda: None) as healthy:
            assert healthy.unhealthy_reason(canary_timeout=15.0) is None
        # Every thread is alive and the directory is right, but no event comes.
        monkeypatch.setattr(local_watch.ArrivalHandler, "on_any_event", _deaf)
        monkeypatch.setattr(local_watch, "DirectoryKqueue", _DeafKqueue)
        with LocalWatch(tmp_path, lambda: None) as deaf:
            reason = deaf.unhealthy_reason(canary_timeout=1.0)
        assert reason is not None
        assert "canary" in reason

    async def test_a_replaced_directory_is_named(self, tmp_path: Path) -> None:
        data = tmp_path / "relay"
        data.mkdir()
        with LocalWatch(data, lambda: None) as watch:
            data.rename(tmp_path / "moved")
            data.mkdir()
            reason = watch.unhealthy_reason(canary_timeout=1.0)
        assert reason is not None
        assert any(w in reason for w in ("inode", "device", "kqueue", "emitter"))

    async def test_start_failures_are_logged_at_every_level(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(failure_log, "REMINDER_SECONDS", 0.0)
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        keeper = WatchKeeper((LocalWatch(blocker / "relay", lambda: None),))
        with caplog.at_level(logging.DEBUG, logger="biff.local_watch"):
            keeper.start()
            for _ in range(11):
                keeper.check()
        errors = [r for r in caplog.records if r.levelno >= logging.WARNING]
        debug = [r for r in caplog.records if r.levelno == logging.DEBUG]
        assert debug  # every retry is recorded
        assert len(errors) >= 2  # the first error, then a periodic reminder
        assert any("NotADirectoryError" in r.getMessage() for r in errors)


class TestPollerRestart:
    async def test_a_dead_poller_is_restarted_with_an_error_log(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess")
        monkeypatch.setattr(poller_supervisor, "_RESTART_DELAY", 0.05)
        runs: list[int] = []

        async def flaky_poller(*_a: object, **_k: object) -> None:
            runs.append(1)
            if len(runs) == 1:
                msg = "poller bug"
                raise RuntimeError(msg)
            await asyncio.Event().wait()

        monkeypatch.setattr(_descriptions, "poll_inbox", flaky_poller)
        state = create_state(
            BiffConfig(user="kai", repo_name="_restart"),
            tmp_path / "relay",
            hostname="h",
            pwd="/p",
            unread_dir=tmp_path / "unread",
        )
        with caplog.at_level(logging.ERROR, logger="biff.server.poller_supervisor"):
            async with StampedClient(
                FastMCPTransport(create_server(state)), caller=caller_for("sess")
            ):
                await _until(lambda: len(runs) >= 2, "the poller was restarted")
        assert any("Poller task died" in r.getMessage() for r in caplog.records)


class TestMalformedMailboxLines:
    async def test_deliver_keeps_a_line_it_cannot_parse(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path)
        await relay.deliver(Message(from_user="eric", to_user="kai", body="first"))
        mailbox = tmp_path / "userinbox-kai.jsonl"
        mailbox.write_text(mailbox.read_text() + "{not json at all\n")
        await relay.deliver(Message(from_user="eric", to_user="kai", body="second"))
        text = mailbox.read_text()
        assert "{not json at all" in text
        assert "first" in text
        assert "second" in text


class TestDeliveryFailureLog:
    async def test_the_log_keeps_the_errno_and_strerror(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        relay = AsyncMock()
        relay.deliver = AsyncMock(side_effect=OSError(28, "No space left on device"))
        with caplog.at_level(logging.WARNING, logger="biff.commands._messaging"):
            delivery = await deliver_with_retry(
                relay,
                from_user="eric",
                from_tty="tty1",
                session_key="eric:t",
                to_user="kai",
                target_repo=None,
                chunks=["hi"],
                display="kai",
            )
        assert "No space left" not in delivery.text  # the user-facing text stays clean
        logged = " ".join(
            r.getMessage() + (str(r.exc_info[1]) if r.exc_info else "")
            for r in caplog.records
        )
        assert "No space left on device" in logged


class TestWallDeadlineFollowsTheRelay:
    async def test_a_display_item_that_expires_early_does_not_disarm_the_wake(
        self, tmp_path: Path
    ) -> None:
        state = _state(tmp_path)
        mcp = create_server(state)
        now = datetime.now(UTC)
        await state.relay.set_wall(
            WallPost(
                text="freeze",
                from_user="eric",
                posted_at=now,
                expires_at=now + timedelta(seconds=30),
            )
        )
        seen = await _descriptions._active_tick(
            mcp, state, _descriptions._Seen(), gate=_descriptions._InboxPokeGate()
        )
        # The monotonic display clock runs ahead of the relay's wall clock.
        real = state.display_queue._clock
        object.__setattr__(state.display_queue, "_clock", lambda: real() + 60.0)
        state.display_queue.snapshot()  # the item is purged
        wall = await mcp.get_tool("wall")
        assert wall is not None
        assert "[WALL]" in (wall.description or "")  # the wall is still shown
        left = _descriptions._seconds_until_deadline(state, seen)
        assert 20 < left < 31  # the deadline follows the relay's expiry


class TestTalkListenWaitsOnAnEvent:
    async def test_a_wake_resumes_the_waiter_at_once(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        waiter = asyncio.create_task(state.wake.nudged(10.0))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        state.wake.wake()
        assert await waiter is True
        assert time.monotonic() - started < 0.2  # not a 0.25 s poll

    async def test_waiting_consumes_nothing_the_poller_needs(
        self, tmp_path: Path
    ) -> None:
        state = _state(tmp_path)
        waiter = asyncio.create_task(state.wake.nudged(10.0))
        await asyncio.sleep(0.05)
        state.wake.wake()
        await waiter
        assert state.wake.is_set  # the poller still sees its wake

    async def test_a_timeout_reports_no_wake(self, tmp_path: Path) -> None:
        assert await _state(tmp_path).wake.nudged(0.05) is False
