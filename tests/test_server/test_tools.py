"""Tests for individual biff MCP tools.

Each tool is tested by calling its underlying function directly via
the registered closure, verifying it reads/writes state correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from fastmcp.tools.tool import FunctionTool

from biff.models import BiffConfig, Message, UserSession
from biff.server.app import create_server
from biff.server.state import ServerState, create_state

if TYPE_CHECKING:
    from fastmcp import FastMCP


def _create_mcp(state: ServerState) -> FastMCP[ServerState]:
    """Create a fully configured MCP server for testing."""
    return create_server(state)


def _get_tool_fn(state: ServerState, tool_name: str):
    """Get the callable for a registered tool by name."""
    mcp = _create_mcp(state)
    tool = mcp._tool_manager._tools[tool_name]
    assert isinstance(tool, FunctionTool)
    return tool.fn


class TestBiffToggleTool:
    def test_disable_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "biff")
        result = fn(enabled=False)
        assert "off" in result
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is False

    def test_enable_messages(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="kai", biff_enabled=False))
        fn = _get_tool_fn(state, "biff")
        result = fn(enabled=True)
        assert "on" in result
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is True

    def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert state.sessions.get_user("kai") is None
        fn = _get_tool_fn(state, "biff")
        fn(enabled=True)
        assert state.sessions.get_user("kai") is not None

    def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        state.sessions.update(UserSession(user="kai", last_active=old_time))
        fn = _get_tool_fn(state, "biff")
        fn(enabled=False)
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.last_active > old_time


class TestFingerTool:
    def test_unknown_user(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "finger")
        result = fn(user="nobody")
        assert "no active session" in result

    def test_shows_plan(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="eric", plan="refactoring auth"))
        fn = _get_tool_fn(state, "finger")
        result = fn(user="eric")
        assert "refactoring auth" in result
        assert "@eric" in result

    def test_shows_availability(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="eric", biff_enabled=False))
        fn = _get_tool_fn(state, "finger")
        result = fn(user="eric")
        assert "messages off" in result

    def test_strips_at_prefix(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="eric", plan="coding"))
        fn = _get_tool_fn(state, "finger")
        result = fn(user="@eric")
        assert "coding" in result
        assert "@eric" in result


class TestWhoTool:
    def test_no_active_sessions(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "who")
        result = fn()
        assert "No active sessions" in result

    def test_lists_active_users(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="kai", plan="coding"))
        state.sessions.update(UserSession(user="eric", plan="reviewing"))
        fn = _get_tool_fn(state, "who")
        result = fn()
        assert "@kai" in result
        assert "@eric" in result
        assert "coding" in result
        assert "reviewing" in result

    def test_excludes_stale_sessions(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=121)
        recent_time = datetime.now(UTC) - timedelta(seconds=119)
        state.sessions.update(UserSession(user="stale", last_active=old_time))
        state.sessions.update(UserSession(user="recent", last_active=recent_time))
        fn = _get_tool_fn(state, "who")
        result = fn()
        assert "@recent" in result
        assert "@stale" not in result


class TestPlanTool:
    def test_sets_plan(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "plan")
        result = fn(message="refactoring auth")
        assert "refactoring auth" in result
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.plan == "refactoring auth"

    def test_updates_existing_plan(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="kai", plan="old plan"))
        fn = _get_tool_fn(state, "plan")
        fn(message="new plan")
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.plan == "new plan"

    def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert state.sessions.get_user("kai") is None
        fn = _get_tool_fn(state, "plan")
        fn(message="starting fresh")
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.plan == "starting fresh"

    def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        state.sessions.update(UserSession(user="kai", last_active=old_time))
        fn = _get_tool_fn(state, "plan")
        fn(message="new work")
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.last_active > old_time


class TestSendMessageTool:
    def test_sends_message(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "send_message")
        result = fn(to="eric", message="hey, PR is ready")
        assert "@eric" in result
        unread = state.messages.get_unread("eric")
        assert len(unread) == 1
        assert unread[0].from_user == "kai"
        assert unread[0].body == "hey, PR is ready"

    def test_strips_at_prefix(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "send_message")
        fn(to="@eric", message="hello")
        unread = state.messages.get_unread("eric")
        assert len(unread) == 1
        assert unread[0].to_user == "eric"

    def test_delivers_when_biff_off(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="eric", biff_enabled=False))
        fn = _get_tool_fn(state, "send_message")
        result = fn(to="eric", message="urgent fix needed")
        assert "@eric" in result
        unread = state.messages.get_unread("eric")
        assert len(unread) == 1

    def test_multiple_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "send_message")
        fn(to="eric", message="first")
        fn(to="eric", message="second")
        unread = state.messages.get_unread("eric")
        assert len(unread) == 2


class TestCheckMessagesTool:
    def test_no_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "check_messages")
        result = fn()
        assert "No new messages" in result

    def test_shows_unread(self, state: ServerState) -> None:
        eric_state = create_state(BiffConfig(user="eric"), state.messages._data_dir)
        eric_send = _get_tool_fn(eric_state, "send_message")
        eric_send(to="kai", message="review my PR please")

        check_fn = _get_tool_fn(state, "check_messages")
        result = check_fn()
        assert "@eric" in result
        assert "review my PR please" in result

    def test_marks_as_read(self, state: ServerState) -> None:
        eric_state = create_state(BiffConfig(user="eric"), state.messages._data_dir)
        eric_send = _get_tool_fn(eric_state, "send_message")
        eric_send(to="kai", message="hello")

        check_fn = _get_tool_fn(state, "check_messages")
        check_fn()

        # Second check should show no new messages
        result = check_fn()
        assert "No new messages" in result

    def test_multiple_senders(self, state: ServerState) -> None:
        eric_state = create_state(BiffConfig(user="eric"), state.messages._data_dir)
        priya_state = create_state(BiffConfig(user="priya"), state.messages._data_dir)
        _get_tool_fn(eric_state, "send_message")(to="kai", message="from eric")
        _get_tool_fn(priya_state, "send_message")(to="kai", message="from priya")

        check_fn = _get_tool_fn(state, "check_messages")
        result = check_fn()
        assert "@eric" in result
        assert "@priya" in result
        assert "from eric" in result
        assert "from priya" in result


class TestToolInteractions:
    """Cross-tool integration tests verifying shared state."""

    def test_plan_then_finger_shows_plan(self, state: ServerState) -> None:
        plan_fn = _get_tool_fn(state, "plan")
        finger_fn = _get_tool_fn(state, "finger")
        plan_fn(message="refactoring auth")
        result = finger_fn(user="kai")
        assert "refactoring auth" in result

    def test_biff_off_then_finger_shows_unavailable(self, state: ServerState) -> None:
        biff_fn = _get_tool_fn(state, "biff")
        finger_fn = _get_tool_fn(state, "finger")
        biff_fn(enabled=False)
        result = finger_fn(user="kai")
        assert "messages off" in result

    def test_plan_then_who_shows_plan(self, state: ServerState) -> None:
        plan_fn = _get_tool_fn(state, "plan")
        who_fn = _get_tool_fn(state, "who")
        plan_fn(message="working on tests")
        result = who_fn()
        assert "@kai" in result
        assert "working on tests" in result


def _tool_description(mcp: FastMCP[ServerState], name: str) -> str:
    """Get a tool's current description from the MCP instance."""
    tool = mcp._tool_manager._tools.get(name)
    assert tool is not None
    assert tool.description is not None
    return tool.description


class TestDynamicDescriptions:
    """Verify check_messages description updates after tool calls."""

    def test_default_description_when_no_messages(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        desc = _tool_description(mcp, "check_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    def test_description_shows_unread_after_send(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        # eric sends kai a message (write directly to shared inbox)
        state.messages.append(
            Message(from_user="eric", to_user="kai", body="auth ready")
        )
        # kai calls any tool — triggers description refresh
        plan_tool = mcp._tool_manager._tools["plan"]
        assert isinstance(plan_tool, FunctionTool)
        plan_tool.fn(message="working")
        desc = _tool_description(mcp, "check_messages")
        assert "1 unread" in desc
        assert "@eric" in desc
        assert "auth ready" in desc

    def test_description_reverts_after_check(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        state.messages.append(Message(from_user="eric", to_user="kai", body="hello"))
        # Trigger refresh via plan
        plan_tool = mcp._tool_manager._tools["plan"]
        assert isinstance(plan_tool, FunctionTool)
        plan_tool.fn(message="working")
        assert "1 unread" in _tool_description(mcp, "check_messages")
        # Now check messages — should clear the description
        check_tool = mcp._tool_manager._tools["check_messages"]
        assert isinstance(check_tool, FunctionTool)
        check_tool.fn()
        desc = _tool_description(mcp, "check_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    def test_description_shows_multiple_senders(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        state.messages.append(
            Message(from_user="eric", to_user="kai", body="PR approved")
        )
        state.messages.append(
            Message(from_user="priya", to_user="kai", body="tests pass")
        )
        # Trigger via who
        who_tool = mcp._tool_manager._tools["who"]
        assert isinstance(who_tool, FunctionTool)
        who_tool.fn()
        desc = _tool_description(mcp, "check_messages")
        assert "2 unread" in desc
        assert "@eric" in desc
        assert "@priya" in desc

    def test_send_message_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        # Another user sends to kai first
        state.messages.append(Message(from_user="eric", to_user="kai", body="hello"))
        # kai sends a message — should also refresh description
        send_tool = mcp._tool_manager._tools["send_message"]
        assert isinstance(send_tool, FunctionTool)
        send_tool.fn(to="eric", message="hey back")
        desc = _tool_description(mcp, "check_messages")
        assert "1 unread" in desc

    def test_finger_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        state.sessions.update(UserSession(user="eric", plan="coding"))
        state.messages.append(
            Message(from_user="eric", to_user="kai", body="look at this")
        )
        finger_tool = mcp._tool_manager._tools["finger"]
        assert isinstance(finger_tool, FunctionTool)
        finger_tool.fn(user="eric")
        desc = _tool_description(mcp, "check_messages")
        assert "1 unread" in desc

    def test_biff_toggle_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        state.messages.append(Message(from_user="eric", to_user="kai", body="urgent"))
        biff_tool = mcp._tool_manager._tools["biff"]
        assert isinstance(biff_tool, FunctionTool)
        biff_tool.fn(enabled=False)
        desc = _tool_description(mcp, "check_messages")
        assert "1 unread" in desc
