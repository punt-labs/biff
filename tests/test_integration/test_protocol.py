"""MCP protocol integration tests.

These tests exercise biff tools through the full MCP protocol path:
Client -> FastMCPTransport -> FastMCP server -> tool closure -> response.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from mcp.types import TextContent

from biff.models import UserSession
from biff.server.state import ServerState

from .conftest import CallToolResult

if TYPE_CHECKING:
    from fastmcp import Client


def _text(result: CallToolResult) -> str:
    """Extract text from the first content block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestToolListing:
    async def test_lists_all_tools(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        names = {t.name for t in tools}
        assert names == {"biff", "finger", "who", "plan"}

    async def test_tools_have_descriptions(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"

    async def test_tools_have_input_schemas(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.inputSchema is not None, f"{tool.name} has no schema"


class TestPlanToolProtocol:
    async def test_set_plan(self, biff_client: Client[Any], state: ServerState) -> None:
        result = await biff_client.call_tool("plan", {"message": "refactoring auth"})
        assert "refactoring auth" in _text(result)
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.plan == "refactoring auth"

    async def test_update_plan(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await biff_client.call_tool("plan", {"message": "first plan"})
        await biff_client.call_tool("plan", {"message": "second plan"})
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.plan == "second plan"


class TestBiffToggleProtocol:
    async def test_disable(self, biff_client: Client[Any], state: ServerState) -> None:
        result = await biff_client.call_tool("biff", {"enabled": False})
        assert "off" in _text(result)
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is False

    async def test_enable(self, biff_client: Client[Any], state: ServerState) -> None:
        await biff_client.call_tool("biff", {"enabled": False})
        result = await biff_client.call_tool("biff", {"enabled": True})
        assert "on" in _text(result)
        session = state.sessions.get_user("kai")
        assert session is not None
        assert session.biff_enabled is True


class TestFingerProtocol:
    async def test_unknown_user(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("finger", {"user": "nobody"})
        assert "no active session" in _text(result)

    async def test_shows_plan(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        state.sessions.update(UserSession(user="eric", plan="reviewing PRs"))
        result = await biff_client.call_tool("finger", {"user": "eric"})
        text = _text(result)
        assert "reviewing PRs" in text
        assert "@eric" in text

    async def test_at_prefix(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        state.sessions.update(UserSession(user="eric", plan="coding"))
        result = await biff_client.call_tool("finger", {"user": "@eric"})
        assert "coding" in _text(result)


class TestWhoProtocol:
    async def test_no_sessions(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("who", {})
        assert "No active sessions" in _text(result)

    async def test_lists_active(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        state.sessions.update(UserSession(user="kai", plan="coding"))
        state.sessions.update(UserSession(user="eric", plan="reviewing"))
        result = await biff_client.call_tool("who", {})
        text = _text(result)
        assert "@kai" in text
        assert "@eric" in text
