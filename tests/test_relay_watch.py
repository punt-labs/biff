"""The local relay's filesystem watch reports arrivals and nothing else (DES-087)."""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from watchdog.events import (
    DirCreatedEvent,
    DirModifiedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
    FileSystemEvent,
)

from biff.local_watch import ArrivalHandler, LocalWatch
from biff.models import Message, WallPost
from biff.relay import LocalRelay, atomic_write

# macOS FSEvents reports a change to an existing file a second or two late.
_DEADLINE = 15.0
# Long enough for a late FSEvents report of a change that must NOT count.
_QUIET = 2.5


@pytest.fixture
def relay(tmp_path: Path) -> LocalRelay:
    return LocalRelay(tmp_path)


class _Counter:
    """Counts callbacks and lets a test await the next one."""

    def __init__(self) -> None:
        self.count = 0
        self._event = asyncio.Event()

    def __call__(self) -> None:
        self.count += 1
        self._event.set()

    async def next(self) -> None:
        self._event.clear()
        async with asyncio.timeout(_DEADLINE):
            await self._event.wait()


class TestIsArrivalFile:
    @pytest.mark.parametrize(
        "name",
        [
            "inbox-kai-tty1.jsonl",
            "userinbox-kai.jsonl",
            "sessions.json",
            "wall.json",
        ],
    )
    def test_arrival_files_are_watched(self, name: str) -> None:
        assert ArrivalHandler.is_arrival_file(name)

    @pytest.mark.parametrize(
        "name",
        [
            "unread.json",
            ".lock",
            "wall.json.4242.tmp",
            "sessions.json.4242.tmp",
            "userinbox-kai.jsonl.4242.tmp",
            "inbox-kai-tty1.jsonl.4242.tmp",
            "sessions.tmp",
            "inbox-kai-tty1.tmp",
            "ttyname-kai-a.lock",
            "remove-kai-tty1",
            "notes.jsonl",
        ],
    )
    def test_other_files_are_not(self, name: str) -> None:
        assert not ArrivalHandler.is_arrival_file(name)


class TestWhatCountsAsAnArrival:
    """The filter, on synthetic events: a real filesystem reports on its own time."""

    def _handler(self) -> ArrivalHandler:
        loop = asyncio.new_event_loop()
        loop.close()
        return ArrivalHandler(loop, lambda: None)

    @pytest.mark.parametrize(
        "event",
        [
            FileModifiedEvent("/relay/.lock"),
            FileModifiedEvent("/relay/unread.json"),
            FileCreatedEvent("/relay/wall.json.12.tmp"),
            DirCreatedEvent("/relay/sub"),
        ],
    )
    def test_these_do_not_wake(self, event: FileSystemEvent) -> None:
        assert not self._handler().is_arrival(event)

    @pytest.mark.parametrize(
        "event",
        [
            FileCreatedEvent("/relay/userinbox-kai.jsonl"),
            FileMovedEvent("/relay/wall.json.12.tmp", "/relay/wall.json"),
            FileDeletedEvent("/relay/sessions.json"),
            DirModifiedEvent("/relay"),
        ],
    )
    def test_these_wake(self, event: FileSystemEvent) -> None:
        assert self._handler().is_arrival(event)


class TestWatch:
    async def test_targeted_message_wakes(self, relay: LocalRelay) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.deliver(
                Message(from_user="eric", to_user="kai:tty1", body="targeted")
            )
            await seen.next()

    async def test_bare_user_message_wakes(self, relay: LocalRelay) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.deliver(Message(from_user="eric", to_user="kai", body="bare"))
            await seen.next()

    async def test_second_message_to_an_existing_mailbox_wakes(
        self, relay: LocalRelay
    ) -> None:
        """An append to an existing file, not just a new file, is reported."""
        await relay.deliver(Message(from_user="eric", to_user="kai", body="first"))
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.deliver(Message(from_user="eric", to_user="kai", body="second"))
            await seen.next()

    async def test_wall_post_wakes(self, relay: LocalRelay) -> None:
        """``atomic_write`` replaces the inode; a directory watch still sees it."""
        seen = _Counter()
        now = datetime.now(UTC)
        with LocalWatch(relay.data_dir, seen):
            await relay.set_wall(
                WallPost(
                    text="freeze",
                    from_user="eric",
                    posted_at=now,
                    expires_at=now + timedelta(hours=1),
                )
            )
            await seen.next()

    async def test_wall_clear_wakes(self, relay: LocalRelay) -> None:
        now = datetime.now(UTC)
        await relay.set_wall(
            WallPost(
                text="freeze",
                from_user="eric",
                posted_at=now,
                expires_at=now + timedelta(hours=1),
            )
        )
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.set_wall(None)
            await seen.next()

    async def test_session_change_wakes(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            atomic_write(tmp_path / "sessions.json", "{}\n")
            await seen.next()

    async def test_no_wake_after_the_watch_exits(self, relay: LocalRelay) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            pass
        await asyncio.sleep(0.2)  # what the watch itself did at start and stop
        seen.count = 0
        await relay.deliver(Message(from_user="eric", to_user="kai", body="late"))
        await asyncio.sleep(_QUIET)
        assert seen.count == 0

    async def test_creates_a_missing_data_directory(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path / "not-yet")
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.deliver(Message(from_user="eric", to_user="kai", body="hi"))
            await seen.next()


class TestAnotherProcess:
    """An arrival written by a different process wakes the watch promptly.

    macOS FSEvents reports another process's writes in batches seconds late
    (measured up to ~9s), so the watch must not rely on it there.
    """

    _PROMPT = 3.0
    _SCRIPT = (
        "import asyncio, sys\n"
        "from datetime import UTC, datetime, timedelta\n"
        "from pathlib import Path\n"
        "from biff.models import Message, WallPost\n"
        "from biff.relay import LocalRelay\n"
        "relay = LocalRelay(Path(sys.argv[1]))\n"
        "now = datetime.now(UTC)\n"
        "end = now + timedelta(hours=1)\n"
        "post = WallPost(text='w', from_user='eric', expires_at=end)\n"
        "async def run():\n"
        "    await relay.deliver(Message(from_user='eric', to_user='kai', body='x'))\n"
        "    await relay.set_wall(post)\n"
        "asyncio.run(run())\n"
    )

    async def test_each_arrival_from_another_process_wakes_promptly(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await asyncio.sleep(0.5)  # let the watch settle
            for _ in range(3):  # a batched backend lags a burst behind
                seen.count = 0
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-c", self._SCRIPT, str(tmp_path)
                )
                await proc.wait()
                async with asyncio.timeout(self._PROMPT):
                    while seen.count == 0:
                        await asyncio.sleep(0.05)
                await asyncio.sleep(0.2)
