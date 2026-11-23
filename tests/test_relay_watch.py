"""The local relay's filesystem watch reports arrivals and nothing else (DES-087)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

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
            "wall.tmp",
            "sessions.tmp",
            "inbox-kai-tty1.tmp",
            "ttyname-kai-a.lock",
            "remove-kai-tty1",
            "notes.jsonl",
        ],
    )
    def test_other_files_are_not(self, name: str) -> None:
        assert not ArrivalHandler.is_arrival_file(name)


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

    async def test_unrelated_files_and_reads_do_not_wake(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        await relay.deliver(Message(from_user="eric", to_user="kai", body="old"))
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            # FSEvents replays the last seconds of history when a watch starts,
            # so the delivery above is reported once; let it land, then count.
            await asyncio.sleep(_QUIET)
            seen.count = 0
            (tmp_path / "unread.json").write_text("{}\n")
            atomic_write(tmp_path / "status.json", "{}\n")
            await relay.fetch_user_inbox("kai")  # a read opens and closes the file
            await asyncio.sleep(_QUIET)
        assert seen.count == 0

    async def test_no_wake_after_the_watch_exits(self, relay: LocalRelay) -> None:
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            pass
        await relay.deliver(Message(from_user="eric", to_user="kai", body="late"))
        await asyncio.sleep(_QUIET)
        assert seen.count == 0

    async def test_creates_a_missing_data_directory(self, tmp_path: Path) -> None:
        relay = LocalRelay(tmp_path / "not-yet")
        seen = _Counter()
        with LocalWatch(relay.data_dir, seen):
            await relay.deliver(Message(from_user="eric", to_user="kai", body="hi"))
            await seen.next()
