"""Tests for dynamic tool description updates and inbox polling."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.models import BiffConfig, Message, UnreadSummary, WallPost
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools._descriptions import (
    _READ_MESSAGES_BASE,
    MAX_UNREAD_COUNT,
    _write_unread_file,
    poll_inbox,
    refresh_read_messages,
)
from biff.server.tools.wall import WALL_BASE_DESCRIPTION

if TYPE_CHECKING:
    from fastmcp import FastMCP

_TEST_REPO = "_test-server"
_KAI_SESSION = "kai:tty1"


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )


class TestRefreshReadMessages:
    async def test_no_messages_uses_base(self, state: ServerState) -> None:
        mcp = create_server(state)
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE

    async def test_unread_shows_count(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="auth module ready",
            )
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        assert "Marks all as read." in desc

    async def test_multiple_unread(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="first")
        )
        await state.relay.deliver(
            Message(from_user="priya", to_user=_KAI_SESSION, body="second")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "2 unread" in desc

    async def test_reverts_to_base_when_cleared(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        # Mark as read
        unread = await state.relay.fetch(_KAI_SESSION)
        await state.relay.mark_read(_KAI_SESSION, [m.id for m in unread])
        await refresh_read_messages(mcp, state)
        assert tool.description == _READ_MESSAGES_BASE

    async def test_ignores_other_users_messages(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="for eric")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE


class TestUnreadFile:
    """Verify unread.json is written for status bar consumption."""

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=tmp_path / "unread.json",
        )

    async def test_writes_unread_file(self, state_with_path: ServerState) -> None:
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="auth ready",
            )
        )
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        assert "preview" not in data

    async def test_writes_zero_when_no_messages(
        self, state_with_path: ServerState
    ) -> None:
        mcp = create_server(state_with_path)
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_reverts_to_zero_after_read(
        self, state_with_path: ServerState
    ) -> None:
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        # Mark as read
        unread = await state_with_path.relay.fetch(_KAI_SESSION)
        await state_with_path.relay.mark_read(_KAI_SESSION, [m.id for m in unread])
        await refresh_read_messages(mcp, state_with_path)
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_clamps_unread_count_at_max(self, tmp_path: Path) -> None:
        path = tmp_path / "unread.json"
        summary = UnreadSummary(count=999)
        _write_unread_file(
            path,
            summary,
            repo_name=_TEST_REPO,
            user="kai",
            tty_name="tty1",
            biff_enabled=True,
        )
        data = json.loads(path.read_text())
        assert data["count"] == MAX_UNREAD_COUNT

    async def test_no_write_when_path_is_none(self, state: ServerState) -> None:
        assert state.unread_path is None
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="test")
        )
        await refresh_read_messages(mcp, state)
        # No error — function completes without attempting file write

    async def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "unread.json"
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(
            config,
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=nested,
        )
        mcp = create_server(state)
        await refresh_read_messages(mcp, state)
        assert nested.exists()


class TestPollInbox:
    """Verify the background inbox poller detects changes and refreshes."""

    _FAST_INTERVAL = 0.01

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=tmp_path / "unread.json",
        )

    async def _run_poller(
        self,
        mcp: FastMCP[ServerState],
        state: ServerState,
        *,
        cycles: int = 5,
    ) -> None:
        """Run the poller for a few cycles then cancel it."""
        task = asyncio.create_task(poll_inbox(mcp, state, interval=self._FAST_INTERVAL))
        await asyncio.sleep(self._FAST_INTERVAL * cycles)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def test_initial_refresh_writes_file(
        self, state_with_path: ServerState
    ) -> None:
        """Poller forces a refresh on its first cycle (last_count=-1)."""
        mcp = create_server(state_with_path)
        await self._run_poller(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_detects_new_message(self, state_with_path: ServerState) -> None:
        """Poller picks up a message added between poll cycles."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial cycle run
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        # Inject a message
        await state_with_path.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="PR ready",
            )
        )
        # Let poller detect the change
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1

    async def test_updates_tool_description(self, state_with_path: ServerState) -> None:
        """Poller updates the read_messages tool description."""
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="lunch?")
        )
        await self._run_poller(mcp, state_with_path)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert "1 unread" in (tool.description or "")

    async def test_skips_refresh_when_unchanged(
        self, state_with_path: ServerState
    ) -> None:
        """Poller does not rewrite the file when count is stable."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial refresh write the file
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        assert state_with_path.unread_path is not None
        mtime_after_initial = state_with_path.unread_path.stat().st_mtime_ns
        # Let several more cycles run — count stays at 0
        await asyncio.sleep(self._FAST_INTERVAL * 10)
        mtime_after_stable = state_with_path.unread_path.stat().st_mtime_ns
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert mtime_after_stable == mtime_after_initial

    async def test_cancellation_is_clean(self, state_with_path: ServerState) -> None:
        """Cancelling the poller task does not raise."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        await asyncio.sleep(self._FAST_INTERVAL * 2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert task.done()

    async def test_poller_detects_wall_post(self, state_with_path: ServerState) -> None:
        """Poller detects a wall posted between cycles and updates tool description."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial cycle run
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        tool = await mcp.get_tool("wall")
        assert tool is not None
        assert tool.description == WALL_BASE_DESCRIPTION

        # Post a wall directly via relay
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        wall = WallPost(
            text="deploy freeze",
            from_user="eric",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await state_with_path.relay.set_wall(wall)
        # Let poller detect the change
        await asyncio.sleep(self._FAST_INTERVAL * 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert tool.description is not None
        assert "deploy freeze" in tool.description
        assert "[WALL]" in tool.description

    async def test_poller_detects_wall_clear(
        self, state_with_path: ServerState
    ) -> None:
        """Poller detects a cleared wall and reverts tool description."""
        mcp = create_server(state_with_path)
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        wall = WallPost(
            text="freeze",
            from_user="eric",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await state_with_path.relay.set_wall(wall)

        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let poller pick up the wall
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        tool = await mcp.get_tool("wall")
        assert tool is not None
        assert "[WALL]" in (tool.description or "")

        # Clear the wall
        await state_with_path.relay.set_wall(None)
        await asyncio.sleep(self._FAST_INTERVAL * 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert tool.description == WALL_BASE_DESCRIPTION
