"""Tests for dynamic tool description updates."""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.models import BiffConfig, Message
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools._descriptions import (
    _CHECK_MESSAGES_BASE,
    refresh_check_messages,
)


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
        state.messages.append(
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
        state.messages.append(Message(from_user="eric", to_user="kai", body="first"))
        state.messages.append(Message(from_user="priya", to_user="kai", body="second"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "2 unread" in desc

    def test_reverts_to_base_when_cleared(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.messages.append(Message(from_user="eric", to_user="kai", body="hello"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        # Mark as read
        unread = state.messages.get_unread("kai")
        state.messages.mark_read([m.id for m in unread])
        refresh_check_messages(mcp, state)
        assert tool.description == _CHECK_MESSAGES_BASE

    def test_ignores_other_users_messages(self, state: ServerState) -> None:
        mcp = create_server(state)
        state.messages.append(Message(from_user="kai", to_user="eric", body="for eric"))
        refresh_check_messages(mcp, state)
        tool = mcp._tool_manager._tools.get("check_messages")
        assert tool is not None
        assert tool.description == _CHECK_MESSAGES_BASE
