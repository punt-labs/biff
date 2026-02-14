"""Tests for individual biff MCP tools.

Each tool is tested by calling its underlying function directly via
the registered closure, verifying it reads/writes state correctly.
"""

from __future__ import annotations

from fastmcp.tools.tool import FunctionTool

from biff.models import UserSession
from biff.server.app import create_server
from biff.server.state import ServerState


def _get_tool_fn(state: ServerState, tool_name: str):
    """Get the callable for a registered tool by name."""
    mcp = create_server(state)
    tool = mcp._tool_manager._tools[tool_name]
    assert isinstance(tool, FunctionTool)
    return tool.fn


class TestMesgTool:
    def test_disable_messages(self, state: ServerState) -> None:
        fn = _get_tool_fn(state, "mesg")
        result = fn(enabled=False)
        assert "off" in result
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is False

    def test_enable_messages(self, state: ServerState) -> None:
        state.sessions.update(UserSession(user="kai", biff_enabled=False))
        fn = _get_tool_fn(state, "mesg")
        result = fn(enabled=True)
        assert "on" in result
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is True

    def test_creates_session_if_missing(self, state: ServerState) -> None:
        assert state.sessions.get_user("kai") is None
        fn = _get_tool_fn(state, "mesg")
        fn(enabled=True)
        assert state.sessions.get_user("kai") is not None


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
