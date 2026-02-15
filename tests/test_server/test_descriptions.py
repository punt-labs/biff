"""Tests for dynamic tool description updates and inbox polling."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.models import BiffConfig, Message
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools._descriptions import (
    _CHECK_MESSAGES_BASE,
    poll_inbox,
    refresh_check_messages,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(BiffConfig(user="kai"), tmp_path)


class TestRefreshCheckMessages:
    def test_no_messages_uses_base(self, state: ServerState) -> None:
        mcp = create_server(state)
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        assert tool.description == _CHECK_MESSAGES_BASE

    def test_unread_shows_count_and_preview(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="auth module ready")
        )
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        assert "@eric" in desc
        assert "auth module ready" in desc
        assert "Marks all as read." in desc

    def test_multiple_unread(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.relay.deliver(Message(from_user="eric", to_user="kai", body="first"))
        state.relay.deliver(Message(from_user="priya", to_user="kai", body="second"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "2 unread" in desc

    def test_reverts_to_base_when_cleared(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.relay.deliver(Message(from_user="eric", to_user="kai", body="hello"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        # Mark as read
        unread = state.relay.fetch("kai")
        state.relay.mark_read("kai", [m.id for m in unread])
        refresh_check_messages(mcp, state)
        assert tool.description == _CHECK_MESSAGES_BASE

    def test_ignores_other_users_messages(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.relay.deliver(Message(from_user="kai", to_user="eric", body="for eric"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        assert tool.description == _CHECK_MESSAGES_BASE


class TestUnreadFile:
    """Verify unread.json is written for status bar consumption."""

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai"),
            tmp_path,
            unread_path=tmp_path / "unread.json",
        )

    def test_writes_unread_file(self, state_with_path: ServerState) -> None:
        mcp = create_server(state_with_path)
        state_with_path.relay.deliver(
            Message(from_user="eric", to_user="kai", body="auth ready")
        )
        refresh_check_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        assert "@eric" in data["preview"]

    def test_writes_zero_when_no_messages(self, state_with_path: ServerState) -> None:
        mcp = create_server(state_with_path)
        refresh_check_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0
        assert data["preview"] == ""

    def test_reverts_to_zero_after_read(self, state_with_path: ServerState) -> None:
        mcp = create_server(state_with_path)
        state_with_path.relay.deliver(
            Message(from_user="eric", to_user="kai", body="hello")
        )
        refresh_check_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        # Mark as read
        unread = state_with_path.relay.fetch("kai")
        state_with_path.relay.mark_read("kai", [m.id for m in unread])
        refresh_check_messages(mcp, state_with_path)
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    def test_no_write_when_path_is_none(self, state: ServerState) -> None:
        assert state.unread_path is None
        mcp = create_server(state)
        state.relay.deliver(Message(from_user="eric", to_user="kai", body="test"))
        refresh_check_messages(mcp, state)
        # No error — function completes without attempting file write

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "unread.json"
        state = create_state(BiffConfig(user="kai"), tmp_path, unread_path=nested)
        mcp = create_server(state)
        refresh_check_messages(mcp, state)
        assert nested.exists()


class TestPollInbox:
    """Verify the background inbox poller detects changes and refreshes."""

    _FAST_INTERVAL = 0.01

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai"),
            tmp_path,
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
        state_with_path.relay.deliver(
            Message(from_user="eric", to_user="kai", body="PR ready")
        )
        # Let poller detect the change
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        assert "@eric" in data["preview"]

    async def test_updates_tool_description(self, state_with_path: ServerState) -> None:
        """Poller updates the check_messages tool description."""
        mcp = create_server(state_with_path)
        state_with_path.relay.deliver(
            Message(from_user="eric", to_user="kai", body="lunch?")
        )
        await self._run_poller(mcp, state_with_path)
        tool = mcp._tool_manager._tools.get("check_messages")
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
