"""Deadline wakes, watch liveness and failure paths of the poller (DES-087).

Real objects throughout: a real ``LocalRelay``, a real filesystem watch, a real
``TalkState``.  The NATS case uses the same fakes as the other poller tests.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff import talk_state
from biff.invite_expiry import InviteExpiry
from biff.local_watch import LocalWatch, WatchKeeper
from biff.models import BiffConfig, Message, UnreadSummary, WallPost
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.app import create_server
from biff.server.state import ServerState
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import poll_inbox
from biff.server.tools.wall import WALL_BASE_DESCRIPTION
from tests._state import create_state

if TYPE_CHECKING:
    from collections.abc import Callable

_REPO = "_test-deadlines"
_DEADLINE = 15.0


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    # The status file lives outside the relay directory, as in production, so
    # the poller's own writes do not wake its watch.
    return create_state(
        BiffConfig(user="kai", repo_name=_REPO),
        tmp_path / "relay",
        tty="tty1",
        hostname="test-host",
        pwd="/test",
        unread_path=tmp_path / "status" / "unread.json",
    )


async def _until(check: Callable[[], bool], what: str) -> None:
    try:
        async with asyncio.timeout(_DEADLINE):
            while not check():
                await asyncio.sleep(0.02)
    except TimeoutError:
        pytest.fail(f"never happened: {what}")


async def _stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


def _wall(seconds: float) -> WallPost:
    now = datetime.now(UTC)
    return WallPost(
        text="freeze",
        from_user="eric",
        posted_at=now,
        expires_at=now + timedelta(seconds=seconds),
    )


def _display_kinds(state: ServerState) -> list[str]:
    assert state.unread_path is not None
    items = json.loads(state.unread_path.read_text())["display_items"]
    return [item["kind"] for item in items]


class TestExpiryClearsWithoutAnotherEvent:
    """A displayed wall or invite clears at its deadline, with no other event."""

    async def test_local_wall_clears_at_its_expiry(self, state: ServerState) -> None:
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            wall = await mcp.get_tool("wall")
            assert wall is not None
            await state.relay.set_wall(_wall(2.0))
            await _until(lambda: "[WALL]" in (wall.description or ""), "wall shown")
            await _until(
                lambda: wall.description == WALL_BASE_DESCRIPTION, "wall cleared"
            )
            await _until(lambda: "wall" not in _display_kinds(state), "status file")
        finally:
            await _stop(task)

    async def test_local_invite_marker_clears_at_the_ttl(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(talk_state, "_EXPIRY", InviteExpiry(1.5))
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            talk = await mcp.get_tool("talk")
            assert talk is not None
            await asyncio.sleep(0.5)
            state.talk.receive(
                {
                    "type": "invite",
                    "from": "eric",
                    "from_key": "eric:abcd",
                    "to_key": state.session_key,
                    "body": "hi",
                }
            )
            state.wake.wake()
            await _until(lambda: "[TALK]" in (talk.description or ""), "marker shown")
            await _until(lambda: "[TALK]" not in (talk.description or ""), "cleared")
        finally:
            await _stop(task)

    async def test_nats_wall_clears_at_its_expiry(self, tmp_path: Path) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=2)
        post = WallPost(
            text="freeze",
            from_user="eric",
            posted_at=datetime.now(UTC),
            expires_at=expires,
        )

        async def get_wall(**_: object) -> WallPost | None:
            return post if datetime.now(UTC) < expires else None

        nc = AsyncMock()

        async def subscribe(subject: str, *, cb: object) -> AsyncMock:
            del subject, cb
            return AsyncMock()

        nc.subscribe = AsyncMock(side_effect=subscribe)
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="t")
        relay.inbox_notify_subject = MagicMock(return_value="i")
        relay.wall_notify_subject = MagicMock(return_value="w")
        relay.get_wall = AsyncMock(side_effect=get_wall)
        relay.get_unread_summary = AsyncMock(return_value=UnreadSummary(count=0))
        relay.get_session = AsyncMock(return_value=None)
        state = create_state(
            BiffConfig(user="kai", repo_name=_REPO),
            tmp_path,
            tty="tty1",
            hostname="h",
            pwd="/t",
            relay=relay,
        )
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            wall = await mcp.get_tool("wall")
            assert wall is not None
            await _until(lambda: "[WALL]" in (wall.description or ""), "wall shown")
            await _until(
                lambda: wall.description == WALL_BASE_DESCRIPTION, "wall cleared"
            )
        finally:
            await _stop(task)


def _fstat(fd: int) -> os.stat_result | None:
    """Stat an open descriptor, or ``None`` if it closed in the meantime."""
    try:
        return os.fstat(fd)
    except OSError:
        return None


class TestWatchLiveness:
    async def test_a_recreated_directory_is_noticed_and_rebuilt(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        data = tmp_path / "relay"
        data.mkdir()
        relay = LocalRelay(data)
        hits: list[int] = []
        keeper = WatchKeeper((LocalWatch(data, lambda: hits.append(1)),))
        keeper.start()
        try:
            assert keeper.check() is False  # healthy: nothing rebuilt
            shutil.rmtree(data)
            data.mkdir()
            with caplog.at_level(logging.WARNING, logger="biff.local_watch"):
                assert keeper.check() is True  # noticed, rebuilt
                assert keeper.check() is False
            hits.clear()
            await relay.deliver(Message(from_user="eric", to_user="kai", body="x"))
            await _until(lambda: bool(hits), "wake after the rebuild")
            # watchdog's own emitter thread logs while the observer is rebuilt
            logging.getLogger("watchdog.observers.inotify").error("emitter inactive")
            noted = [
                r
                for r in caplog.records
                if r.name == "biff.local_watch" and r.levelno >= logging.WARNING
            ]
            assert [r.levelno for r in noted] == [logging.ERROR, logging.WARNING]
            assert "failed" in noted[0].getMessage()  # onset, then recovery
            assert "recovered" in noted[1].getMessage()
        finally:
            keeper.stop()

    async def test_the_watch_holds_its_directory_open_so_its_inode_is_not_reused(
        self, tmp_path: Path
    ) -> None:
        """A freed inode is reused on Linux, so ``st_ino`` alone can match.

        A recreated directory then looks like the one that was watched, the
        replacement goes unnoticed, and the dead inotify watch stays.  While a
        descriptor on the directory is open the kernel cannot free its inode.
        """
        data = tmp_path / "relay"
        data.mkdir()
        watch = LocalWatch(data, lambda: None)
        watch.start()
        try:
            held = data.stat()
            open_dirs = {
                (info.st_dev, info.st_ino)
                for name in (p.name for p in Path("/dev/fd").iterdir())
                if (info := _fstat(int(name))) is not None
                and stat.S_ISDIR(info.st_mode)
            }
            assert (held.st_dev, held.st_ino) in open_dirs
        finally:
            watch.close()
        shutil.rmtree(data)

    async def test_a_failed_start_leaks_no_thread_and_raises(
        self, tmp_path: Path
    ) -> None:
        not_a_directory = tmp_path / "file"
        not_a_directory.write_text("x")
        before = {t.ident for t in threading.enumerate()}
        watch = LocalWatch(not_a_directory / "relay", lambda: None)
        with pytest.raises(OSError, match=r"."):
            watch.start()
        assert {t.ident for t in threading.enumerate()} <= before

    async def test_poller_survives_a_watch_that_cannot_start(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("x")
        state = create_state(
            BiffConfig(user="kai", repo_name=_REPO),
            tmp_path / "state",
            tty="tty1",
            hostname="h",
            pwd="/t",
            relay=LocalRelay(blocker / "relay"),
        )
        mcp = create_server(state)
        with caplog.at_level(logging.ERROR, logger="biff.local_watch"):
            task = asyncio.create_task(poll_inbox(mcp, state))
            await asyncio.sleep(0.5)
            assert not task.done()  # still running, retrying on the liveness pass
            await _stop(task)
        assert any(str(blocker) in r.getMessage() for r in caplog.records)

    async def test_descriptor_use_does_not_grow_with_the_file_count(
        self, tmp_path: Path
    ) -> None:
        def watch_cost(directory: Path) -> int:
            before = len(list(Path("/dev/fd").iterdir()))
            with LocalWatch(directory, lambda: None):
                return len(list(Path("/dev/fd").iterdir())) - before

        empty = tmp_path / "empty"
        empty.mkdir()
        full = tmp_path / "full"
        full.mkdir()
        for i in range(300):
            (full / f"inbox-kai-{i:04d}.jsonl").write_text("")
        # One watch per directory, not one per file.
        assert watch_cost(full) - watch_cost(empty) <= 1


class TestTickFailure:
    async def test_failure_warns_once_and_the_liveness_pass_retries(
        self,
        state: ServerState,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        monkeypatch.setattr(_descriptions, "_WATCH_LIVENESS_INTERVAL", 0.2)
        real = state.relay.get_unread_summary
        calls = 0

        async def flaky(session_key: str) -> UnreadSummary:
            nonlocal calls
            calls += 1
            if calls <= 3:
                msg = "disk gone"
                raise OSError(msg)
            return await real(session_key)

        monkeypatch.setattr(state.relay, "get_unread_summary", flaky)
        mcp = create_server(state)
        with caplog.at_level(logging.DEBUG, logger=_descriptions.logger.name):
            task = asyncio.create_task(poll_inbox(mcp, state))
            await _until(lambda: calls > 3, "the liveness pass retried")
            await asyncio.sleep(0.3)
            await _stop(task)
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.WARNING and "tick" in r.getMessage()
        ]
        assert len(warnings) == 2  # onset once, recovery once


class TestSentinelReapIsEventDriven:
    async def test_a_new_sentinel_wakes_the_poller_and_is_reaped(
        self, state: ServerState, tmp_path: Path
    ) -> None:
        sentinels = tmp_path / "sentinels"
        reaped: list[list[str]] = []

        async def reap() -> None:
            reaped.append(sorted(p.name for p in sentinels.glob("*")))

        mcp = create_server(state)
        task = asyncio.create_task(
            poll_inbox(mcp, state, reap=reap, watch_dirs=(sentinels,))
        )
        try:
            await _until(lambda: bool(reaped), "reaped at startup")
            (sentinels / "remove-kai-tty9").write_text("kai:tty9")
            await _until(
                lambda: any("remove-kai-tty9" in seen for seen in reaped),
                "reaped on the sentinel's arrival",
            )
        finally:
            await _stop(task)

    async def test_a_failing_reap_is_logged_and_does_not_end_the_poller(
        self, state: ServerState, caplog: pytest.LogCaptureFixture
    ) -> None:
        async def reap() -> None:
            msg = "boom"
            raise OSError(msg)

        mcp = create_server(state)
        with caplog.at_level(logging.WARNING, logger=_descriptions.logger.name):
            task = asyncio.create_task(poll_inbox(mcp, state, reap=reap))
            await _until(lambda: bool(caplog.records), "the failure was logged")
            assert not task.done()
            await _stop(task)
