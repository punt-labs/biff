"""E2E test: KV watcher survives snapshot-done and receives live updates (biff-udp).

Exercises ``_run_kv_watch`` against a real local NATS server to prove that
post-snapshot KV writes are delivered without restarting the watcher.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.server.app import _run_kv_watch, create_server
from biff.server.state import create_state

pytestmark = pytest.mark.nats

_TEST_REPO = "_test-kv-survival"


class TestKvWatchSurvivalE2E:
    """_run_kv_watch receives KV updates written after the snapshot completes."""

    async def test_post_snapshot_wall_update_detected(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        """Wall KV entry after snapshot triggers refresh_wall."""
        config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        state = create_state(
            config, tmp_path, tty="tty1", hostname="test-host", pwd="/test"
        )
        relay = state.relay
        assert isinstance(relay, NatsRelay)

        # get_kv() lazily connects and provisions the KV bucket
        kv = await relay.get_kv()
        wall_key = NatsRelay.wall_kv_key(_TEST_REPO)
        await kv.put(wall_key, b"initial-wall")  # pyright: ignore[reportUnknownMemberType]

        shutdown = asyncio.Event()
        wall_refreshed = asyncio.Event()
        refresh_count = 0

        async def _mock_refresh_wall(*_args: object, **_kwargs: object) -> None:
            nonlocal refresh_count
            refresh_count += 1
            # The first refresh is from the snapshot entry.
            # The second is the post-snapshot live update.
            if refresh_count >= 2:
                wall_refreshed.set()

        mcp = create_server(state)

        async def _write_after_snapshot() -> None:
            # Wait a bit for the watcher to drain its snapshot
            await asyncio.sleep(0.5)
            # Write a new wall value — this should be detected as a live update
            await kv.put(wall_key, b"post-snapshot-wall")  # pyright: ignore[reportUnknownMemberType]

        async def _shutdown_after_detection() -> None:
            try:
                await asyncio.wait_for(wall_refreshed.wait(), timeout=10.0)
            finally:
                shutdown.set()

        writer_task = asyncio.create_task(_write_after_snapshot())
        shutdown_task = asyncio.create_task(_shutdown_after_detection())

        cache: dict[str, object] = {}

        with patch("biff.server.app.refresh_wall", _mock_refresh_wall):
            await _run_kv_watch(
                mcp,
                relay,
                state,
                shutdown,
                cache,  # type: ignore[arg-type]
            )

        await writer_task
        await shutdown_task

        assert wall_refreshed.is_set(), (
            f"Post-snapshot wall update not detected (refresh_count={refresh_count}). "
            "The watcher likely terminated on the snapshot-done marker."
        )

        await relay.disconnect()

    async def test_post_snapshot_session_update_cached(
        self, nats_server: str, tmp_path: Path
    ) -> None:
        """A session KV entry written after snapshot is cached by _handle_kv_entry."""
        from biff.models import UserSession

        config = BiffConfig(user="kai", repo_name=_TEST_REPO, relay_url=nats_server)
        state = create_state(
            config, tmp_path, tty="tty1", hostname="test-host", pwd="/test"
        )
        relay = state.relay
        assert isinstance(relay, NatsRelay)

        kv = await relay.get_kv()

        # Seed with kai's session so snapshot has content
        kai_key = f"{_TEST_REPO}.kai.tty1"
        kai_session = UserSession(
            user="kai", tty="tty1", hostname="test-host", pwd="/test"
        )
        await kv.put(kai_key, kai_session.model_dump_json().encode())  # pyright: ignore[reportUnknownMemberType]

        shutdown = asyncio.Event()
        cache: dict[str, UserSession] = {}

        # We'll write eric's session after the snapshot
        eric_key = f"{_TEST_REPO}.eric.tty2"
        eric_session = UserSession(
            user="eric", tty="tty2", hostname="test-host", pwd="/test"
        )

        async def _write_eric_after_snapshot() -> None:
            await asyncio.sleep(0.5)
            await kv.put(eric_key, eric_session.model_dump_json().encode())  # pyright: ignore[reportUnknownMemberType]

        async def _shutdown_when_cached() -> None:
            # Poll the cache for eric's session
            for _ in range(100):
                if "eric:tty2" in cache:
                    break
                await asyncio.sleep(0.1)
            shutdown.set()

        mcp = create_server(state)
        writer_task = asyncio.create_task(_write_eric_after_snapshot())
        shutdown_task = asyncio.create_task(_shutdown_when_cached())

        await _run_kv_watch(mcp, relay, state, shutdown, cache)

        await writer_task
        await shutdown_task

        assert "eric:tty2" in cache, (
            "Post-snapshot session entry was not cached. "
            "The watcher likely terminated on the snapshot-done marker."
        )
        assert cache["eric:tty2"].user == "eric"

        await relay.disconnect()
