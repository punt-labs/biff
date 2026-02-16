"""Tests for individual biff MCP tools.

Each tool is tested by calling its underlying function directly via
the registered closure, verifying it reads/writes state correctly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    async def test_disable_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "mesg")
        result = await fn(enabled=False)
        assert "is n" in result
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.biff_enabled is False

    async def test_enable_messages(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="kai", biff_enabled=False))
        fn = _get_tool_fn(state, "mesg")
        result = await fn(enabled=True)
        assert "is y" in result
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.biff_enabled is True

    async def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert await state.relay.get_session("kai") is None
        fn = _get_tool_fn(state, "mesg")
        await fn(enabled=True)
        assert await state.relay.get_session("kai") is not None

    async def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await state.relay.update_session(UserSession(user="kai", last_active=old_time))
        fn = _get_tool_fn(state, "mesg")
        await fn(enabled=False)
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.last_active > old_time


class TestFingerTool:
    async def test_unknown_user(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="nobody")
        assert "Never logged in" in result

    async def test_shows_plan(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", plan="refactoring auth")
        )
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "refactoring auth" in result
        assert "Login: eric" in result

    async def test_shows_availability(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", biff_enabled=False))
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Messages: off" in result

    async def test_strips_at_prefix(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", plan="coding"))
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="@eric")
        assert "coding" in result
        assert "Login: eric" in result

    async def test_shows_display_name(self, state: ServerState) -> None:
        await state.relay.update_session(
            UserSession(user="eric", display_name="Eric Alvarez", plan="debugging")
        )
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Name: Eric Alvarez" in result
        assert "Login: eric" in result
        assert "Messages: on" in result

    async def test_omits_name_when_empty(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", plan="coding"))
        fn = _get_tool_fn(state, "finger")
        result = await fn(user="eric")
        assert "Name:" not in result
        assert "Messages: on" in result


class TestWhoTool:
    async def test_always_includes_self(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "@kai" in result

    async def test_lists_users(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="kai", plan="coding"))
        await state.relay.update_session(UserSession(user="eric", plan="reviewing"))
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "@kai" in result
        assert "@eric" in result
        assert "coding" in result
        assert "reviewing" in result

    async def test_shows_idle_time(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(hours=3)
        await state.relay.update_session(
            UserSession(user="eric", plan="reviewing", last_active=old_time)
        )
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "3h" in result

    async def test_includes_all_sessions(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(days=2)
        recent_time = datetime.now(UTC) - timedelta(seconds=30)
        await state.relay.update_session(
            UserSession(user="old", last_active=old_time, plan="vacation")
        )
        await state.relay.update_session(
            UserSession(user="recent", last_active=recent_time, plan="coding")
        )
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "@old" in result
        assert "@recent" in result
        assert "2d" in result

    async def test_sorted_by_username(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="zara", plan="testing"))
        await state.relay.update_session(UserSession(user="alice", plan="coding"))
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert result.index("@alice") < result.index("@zara")

    async def test_sanitizes_pipe_in_plan(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="kai", plan="fix | deploy"))
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "|" not in result
        assert "fix / deploy" in result

    async def test_sanitizes_newline_in_plan(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="kai", plan="line1\nline2"))
        fn = _get_tool_fn(state, "who")
        result = await fn()
        assert "\n" not in result.split(" | ")[0]
        assert "line1 line2" in result


class TestPlanTool:
    async def test_sets_plan(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "plan")
        result = await fn(message="refactoring auth")
        assert "refactoring auth" in result
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.plan == "refactoring auth"

    async def test_updates_existing_plan(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="kai", plan="old plan"))
        fn = _get_tool_fn(state, "plan")
        await fn(message="new plan")
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.plan == "new plan"

    async def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert await state.relay.get_session("kai") is None
        fn = _get_tool_fn(state, "plan")
        await fn(message="starting fresh")
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.plan == "starting fresh"

    async def test_updates_last_active(self, state: ServerState) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await state.relay.update_session(UserSession(user="kai", last_active=old_time))
        fn = _get_tool_fn(state, "plan")
        await fn(message="new work")
        session = await state.relay.get_session("kai")
        assert session is not None
        assert session.last_active > old_time


class TestSendMessageTool:
    async def test_sends_message(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "write")
        result = await fn(to="eric", message="hey, PR is ready")
        assert "@eric" in result
        unread = await state.relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].from_user == "kai"
        assert unread[0].body == "hey, PR is ready"

    async def test_strips_at_prefix(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "write")
        await fn(to="@eric", message="hello")
        unread = await state.relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].to_user == "eric"

    async def test_delivers_when_biff_off(self, state: ServerState) -> None:
        await state.relay.update_session(UserSession(user="eric", biff_enabled=False))
        fn = _get_tool_fn(state, "write")
        result = await fn(to="eric", message="urgent fix needed")
        assert "@eric" in result
        unread = await state.relay.fetch("eric")
        assert len(unread) == 1

    async def test_multiple_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "write")
        await fn(to="eric", message="first")
        await fn(to="eric", message="second")
        unread = await state.relay.fetch("eric")
        assert len(unread) == 2


class TestCheckMessagesTool:
    async def test_no_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "read_messages")
        result = await fn()
        assert "No new messages" in result

    async def test_shows_unread(self, state: ServerState, tmp_path: Path) -> None:
        eric_state = create_state(BiffConfig(user="eric"), tmp_path)
        eric_send = _get_tool_fn(eric_state, "write")
        await eric_send(to="kai", message="review my PR please")

        check_fn = _get_tool_fn(state, "read_messages")
        result = await check_fn()
        assert "From eric" in result
        assert "review my PR please" in result

    async def test_marks_as_read(self, state: ServerState, tmp_path: Path) -> None:
        eric_state = create_state(BiffConfig(user="eric"), tmp_path)
        eric_send = _get_tool_fn(eric_state, "write")
        await eric_send(to="kai", message="hello")

        check_fn = _get_tool_fn(state, "read_messages")
        await check_fn()

        # Second check should show no new messages
        result = await check_fn()
        assert "No new messages" in result

    async def test_multiple_senders(self, state: ServerState, tmp_path: Path) -> None:
        eric_state = create_state(BiffConfig(user="eric"), tmp_path)
        priya_state = create_state(BiffConfig(user="priya"), tmp_path)
        await _get_tool_fn(eric_state, "write")(to="kai", message="from eric")
        await _get_tool_fn(priya_state, "write")(to="kai", message="from priya")

        check_fn = _get_tool_fn(state, "read_messages")
        result = await check_fn()
        assert "From eric" in result
        assert "From priya" in result
        assert "from eric" in result
        assert "from priya" in result


class TestToolInteractions:
    """Cross-tool integration tests verifying shared state."""

    async def test_plan_then_finger_shows_plan(self, state: ServerState) -> None:
        plan_fn = _get_tool_fn(state, "plan")
        finger_fn = _get_tool_fn(state, "finger")
        await plan_fn(message="refactoring auth")
        result = await finger_fn(user="kai")
        assert "refactoring auth" in result

    async def test_biff_off_then_finger_shows_unavailable(
        self, state: ServerState
    ) -> None:
        biff_fn = _get_tool_fn(state, "mesg")
        finger_fn = _get_tool_fn(state, "finger")
        await biff_fn(enabled=False)
        result = await finger_fn(user="kai")
        assert "Messages: off" in result

    async def test_plan_then_who_shows_plan(self, state: ServerState) -> None:
        plan_fn = _get_tool_fn(state, "plan")
        who_fn = _get_tool_fn(state, "who")
        await plan_fn(message="working on tests")
        result = await who_fn()
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

    async def test_default_description_when_no_messages(
        self, state: ServerState
    ) -> None:
        mcp = _create_mcp(state)
        desc = _tool_description(mcp, "read_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    async def test_description_shows_unread_after_send(
        self, state: ServerState
    ) -> None:
        mcp = _create_mcp(state)
        # eric sends kai a message (write directly to shared inbox)
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="auth ready")
        )
        # kai calls any tool — triggers description refresh
        plan_tool = mcp._tool_manager._tools["plan"]
        assert isinstance(plan_tool, FunctionTool)
        await plan_tool.fn(message="working")
        desc = _tool_description(mcp, "read_messages")
        assert "1 unread" in desc
        assert "@eric" in desc
        assert "auth ready" in desc

    async def test_description_reverts_after_check(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="hello")
        )
        # Trigger refresh via plan
        plan_tool = mcp._tool_manager._tools["plan"]
        assert isinstance(plan_tool, FunctionTool)
        await plan_tool.fn(message="working")
        assert "1 unread" in _tool_description(mcp, "read_messages")
        # Now check messages — should clear the description
        check_tool = mcp._tool_manager._tools["read_messages"]
        assert isinstance(check_tool, FunctionTool)
        await check_tool.fn()
        desc = _tool_description(mcp, "read_messages")
        assert desc == "Check your inbox for new messages. Marks all as read."

    async def test_description_shows_multiple_senders(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="PR approved")
        )
        await state.relay.deliver(
            Message(from_user="priya", to_user="kai", body="tests pass")
        )
        # Trigger via who
        who_tool = mcp._tool_manager._tools["who"]
        assert isinstance(who_tool, FunctionTool)
        await who_tool.fn()
        desc = _tool_description(mcp, "read_messages")
        assert "2 unread" in desc
        assert "@eric" in desc
        assert "@priya" in desc

    async def test_send_message_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        # Another user sends to kai first
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="hello")
        )
        # kai sends a message — should also refresh description
        send_tool = mcp._tool_manager._tools["write"]
        assert isinstance(send_tool, FunctionTool)
        await send_tool.fn(to="eric", message="hey back")
        desc = _tool_description(mcp, "read_messages")
        assert "1 unread" in desc

    async def test_finger_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.update_session(UserSession(user="eric", plan="coding"))
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="look at this")
        )
        finger_tool = mcp._tool_manager._tools["finger"]
        assert isinstance(finger_tool, FunctionTool)
        await finger_tool.fn(user="eric")
        desc = _tool_description(mcp, "read_messages")
        assert "1 unread" in desc

    async def test_biff_toggle_triggers_refresh(self, state: ServerState) -> None:
        mcp = _create_mcp(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user="kai", body="urgent")
        )
        biff_tool = mcp._tool_manager._tools["mesg"]
        assert isinstance(biff_tool, FunctionTool)
        await biff_tool.fn(enabled=False)
        desc = _tool_description(mcp, "read_messages")
        assert "1 unread" in desc
