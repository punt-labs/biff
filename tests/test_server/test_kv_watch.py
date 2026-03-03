"""Unit tests for _run_kv_watch — snapshot-done survival (biff-udp).

Verifies that the KV watcher loop continues processing entries after
the snapshot-done ``None`` marker, rather than terminating the iterator.
Uses a mock watcher to isolate the iteration logic from NATS.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from biff.models import BiffConfig, UserSession
from biff.server.app import _run_kv_watch
from biff.server.state import ServerState, create_state

_TEST_REPO = "_test-kv-watch"

# Sentinel for scripting a TimeoutError in FakeWatcher
_TIMEOUT = object()

type ScriptItem = FakeKVEntry | None | object


@dataclass
class FakeKVEntry:
    """Minimal stand-in for a nats KeyValue.Entry."""

    key: str
    value: bytes | None = None
    operation: str | None = None  # None = PUT, "DEL", "PURGE"


class FakeWatcher:
    """Mock KV watcher that yields a scripted sequence from ``updates()``.

    Script items:
    - ``FakeKVEntry``: returned as-is (simulates a KV update)
    - ``None``: returned as-is (simulates snapshot-done marker)
    - ``_TIMEOUT``: raises ``TimeoutError`` (simulates nats idle timeout)

    After the script is exhausted, blocks until shutdown.
    """

    def __init__(self, script: Sequence[ScriptItem], shutdown: asyncio.Event) -> None:
        self._script = list(script)
        self._shutdown = shutdown
        self._index = 0
        self.stopped = False

    async def updates(self, timeout: float = 5.0) -> FakeKVEntry | None:
        if self._index < len(self._script):
            entry = self._script[self._index]
            self._index += 1
            if entry is _TIMEOUT:
                raise TimeoutError("nats: timeout")
            return entry  # type: ignore[return-value]
        # Script exhausted — wait until shutdown to simulate idle watcher
        await self._shutdown.wait()
        return None

    async def stop(self) -> None:
        self.stopped = True


@dataclass
class FakeKV:
    """Mock KV bucket that returns a pre-built watcher."""

    watcher: FakeWatcher

    async def watchall(self) -> FakeWatcher:
        return self.watcher


@dataclass
class FakeNatsRelay:
    """Minimal mock of NatsRelay for _run_kv_watch."""

    kv: FakeKV

    async def get_kv(self) -> FakeKV:
        return self.kv

    @staticmethod
    def wall_kv_key(repo_name: str) -> str:
        return f"{repo_name}.wall"


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )


class TestKvWatchSnapshotSurvival:
    """The watcher loop must survive past the snapshot-done None marker."""

    async def test_processes_entries_after_snapshot_done(
        self, state: ServerState
    ) -> None:
        """Entries arriving after None (snapshot-done) are still processed.

        This is the core regression test for biff-udp: the old ``async for``
        pattern terminated on None, so post-snapshot entries were missed.
        """
        shutdown = asyncio.Event()
        wall_key = f"{_TEST_REPO}.wall"
        session_key = f"{_TEST_REPO}.kai.tty1"
        session_json = (
            UserSession(
                user="kai",
                tty="tty1",
                hostname="test-host",
                pwd="/test",
            )
            .model_dump_json()
            .encode()
        )

        # Script: snapshot entry → None (snapshot done) → post-snapshot entries
        script: list[FakeKVEntry | None] = [
            FakeKVEntry(key=session_key, value=session_json),  # snapshot
            None,  # snapshot-done marker
            FakeKVEntry(key=wall_key, value=b"post-snapshot wall"),  # live update
            FakeKVEntry(key=session_key, value=session_json),  # live session update
        ]

        watcher = FakeWatcher(script, shutdown)
        fake_kv = FakeKV(watcher=watcher)
        fake_relay = FakeNatsRelay(kv=fake_kv)

        cache: dict[str, UserSession] = {}
        wall_refreshed = asyncio.Event()

        async def _mock_refresh_wall(*_args: object, **_kwargs: object) -> None:
            wall_refreshed.set()

        # Signal shutdown after the watcher drains its script and blocks
        async def _shutdown_after_drain() -> None:
            # Wait for the wall refresh (proves post-snapshot entry was processed)
            await asyncio.wait_for(wall_refreshed.wait(), timeout=5.0)
            # Give time for the session entry after wall to be processed
            await asyncio.sleep(0.05)
            shutdown.set()

        mcp = AsyncMock()
        shutdown_task = asyncio.create_task(_shutdown_after_drain())

        with patch("biff.server.app.refresh_wall", _mock_refresh_wall):
            await _run_kv_watch(
                mcp,
                fake_relay,  # type: ignore[arg-type]
                state,
                shutdown,
                cache,
            )

        await shutdown_task

        # The wall entry after None was processed
        assert wall_refreshed.is_set(), (
            "Wall refresh not triggered for post-snapshot entry"
        )
        # The session entry after None was cached
        assert "kai:tty1" in cache, "Session entry after snapshot-done was not cached"
        assert watcher.stopped, "Watcher was not stopped in finally block"

    async def test_none_does_not_terminate_loop(self, state: ServerState) -> None:
        """Multiple None entries (timeouts) don't exit the loop."""
        shutdown = asyncio.Event()

        wall_key = f"{_TEST_REPO}.wall"

        # Script: three Nones, then a wall entry, then idle
        script: list[FakeKVEntry | None] = [
            None,
            None,
            None,
            FakeKVEntry(key=wall_key, value=b"after-timeouts"),
        ]

        watcher = FakeWatcher(script, shutdown)
        fake_kv = FakeKV(watcher=watcher)
        fake_relay = FakeNatsRelay(kv=fake_kv)

        cache: dict[str, UserSession] = {}
        wall_refreshed = asyncio.Event()

        async def _mock_refresh_wall(*_args: object, **_kwargs: object) -> None:
            wall_refreshed.set()

        async def _shutdown_after_wall() -> None:
            await asyncio.wait_for(wall_refreshed.wait(), timeout=5.0)
            shutdown.set()

        mcp = AsyncMock()
        shutdown_task = asyncio.create_task(_shutdown_after_wall())

        with patch("biff.server.app.refresh_wall", _mock_refresh_wall):
            await _run_kv_watch(
                mcp,
                fake_relay,  # type: ignore[arg-type]
                state,
                shutdown,
                cache,
            )

        await shutdown_task
        assert wall_refreshed.is_set(), (
            "Wall entry after multiple Nones was not processed"
        )

    async def test_timeout_does_not_terminate_loop(self, state: ServerState) -> None:
        """TimeoutError from watcher.updates() is caught and the loop continues.

        Real nats.py raises nats.errors.TimeoutError (a TimeoutError subclass)
        when no updates arrive within the timeout window.  The loop must
        survive these without restarting the watcher.
        """
        shutdown = asyncio.Event()
        wall_key = f"{_TEST_REPO}.wall"

        # Script: timeout → timeout → wall entry
        script: list[ScriptItem] = [
            _TIMEOUT,
            _TIMEOUT,
            FakeKVEntry(key=wall_key, value=b"after-timeouts"),
        ]

        watcher = FakeWatcher(script, shutdown)
        fake_kv = FakeKV(watcher=watcher)
        fake_relay = FakeNatsRelay(kv=fake_kv)

        cache: dict[str, UserSession] = {}
        wall_refreshed = asyncio.Event()

        async def _mock_refresh_wall(*_args: object, **_kwargs: object) -> None:
            wall_refreshed.set()

        async def _shutdown_after_wall() -> None:
            await asyncio.wait_for(wall_refreshed.wait(), timeout=5.0)
            shutdown.set()

        mcp = AsyncMock()
        shutdown_task = asyncio.create_task(_shutdown_after_wall())

        with patch("biff.server.app.refresh_wall", _mock_refresh_wall):
            await _run_kv_watch(
                mcp,
                fake_relay,  # type: ignore[arg-type]
                state,
                shutdown,
                cache,
            )

        await shutdown_task
        assert wall_refreshed.is_set(), (
            "Wall entry after TimeoutErrors was not processed"
        )

    async def test_shutdown_exits_cleanly(self, state: ServerState) -> None:
        """Setting shutdown causes the loop to exit without processing more entries."""
        shutdown = asyncio.Event()
        shutdown.set()  # Already signalled

        watcher = FakeWatcher([], shutdown)
        fake_kv = FakeKV(watcher=watcher)
        fake_relay = FakeNatsRelay(kv=fake_kv)

        cache: dict[str, UserSession] = {}
        mcp = AsyncMock()

        # Should return immediately
        await asyncio.wait_for(
            _run_kv_watch(
                mcp,
                fake_relay,  # type: ignore[arg-type]
                state,
                shutdown,
                cache,
            ),
            timeout=2.0,
        )
        assert watcher.stopped
