"""MCP protocol integration tests.

These tests exercise biff tools through the full MCP protocol path:
Client -> FastMCPTransport -> FastMCP server -> tool closure -> response.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import pytest
from fastmcp import Client
from fastmcp.client.messages import MessageHandler
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent

from biff.models import UserSession
from biff.server.app import create_server
from biff.server.state import ServerState

from .conftest import CallToolResult

if TYPE_CHECKING:
    from mcp import types as mcp_types

_ERIC_TTY = "tty2"


class _NotificationTracker(MessageHandler):
    """Message handler that counts tools/list_changed notifications."""

    def __init__(self) -> None:
        self.tool_list_changed_count = 0

    async def on_tool_list_changed(  # pyright: ignore[reportUnusedParameter]
        self, message: mcp_types.ToolListChangedNotification
    ) -> None:
        self.tool_list_changed_count += 1


@pytest.fixture
async def tracked_client(
    state: ServerState,
) -> AsyncIterator[tuple[Client[Any], _NotificationTracker]]:
    """MCP client with notification tracking."""
    from biff.server.tools._descriptions import _reset_session

    _reset_session()
    tracker = _NotificationTracker()
    mcp = create_server(state)
    async with Client(FastMCPTransport(mcp), message_handler=tracker) as client:
        yield client, tracker
    _reset_session()


def _text(result: CallToolResult) -> str:
    """Extract text from the first content block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestToolListing:
    async def test_lists_all_tools(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        names = {t.name for t in tools}
        expected = {
            "mesg",
            "read_messages",
            "finger",
            "write",
            "who",
            "plan",
            "tty",
            "last",
        }
        assert names == expected

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
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "refactoring auth"

    async def test_update_plan(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await biff_client.call_tool("plan", {"message": "first plan"})
        await biff_client.call_tool("plan", {"message": "second plan"})
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.plan == "second plan"


class TestBiffToggleProtocol:
    async def test_disable(self, biff_client: Client[Any], state: ServerState) -> None:
        result = await biff_client.call_tool("mesg", {"enabled": False})
        assert "is n" in _text(result)
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.biff_enabled is False

    async def test_enable(self, biff_client: Client[Any], state: ServerState) -> None:
        await biff_client.call_tool("mesg", {"enabled": False})
        result = await biff_client.call_tool("mesg", {"enabled": True})
        assert "is y" in _text(result)
        session = await state.relay.get_session(state.session_key)
        assert session is not None
        assert session.biff_enabled is True


class TestFingerProtocol:
    async def test_unknown_user(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("finger", {"user": "nobody"})
        assert "Never logged in" in _text(result)

    async def test_shows_plan(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="reviewing PRs")
        )
        result = await biff_client.call_tool("finger", {"user": "eric"})
        text = _text(result)
        assert "reviewing PRs" in text
        assert "Login: eric" in text

    async def test_at_prefix(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="coding")
        )
        result = await biff_client.call_tool("finger", {"user": "@eric"})
        assert "coding" in _text(result)


class TestWhoProtocol:
    async def test_always_includes_self(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("who", {})
        assert "@kai" in _text(result)

    async def test_lists_active(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await state.relay.update_session(
            UserSession(user="kai", tty="tty1", plan="coding")
        )
        await state.relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="reviewing")
        )
        result = await biff_client.call_tool("who", {})
        text = _text(result)
        assert "@kai" in text
        assert "@eric" in text


class TestSendMessageProtocol:
    async def test_send(self, biff_client: Client[Any], state: ServerState) -> None:
        result = await biff_client.call_tool(
            "write",
            {"to": f"eric:{_ERIC_TTY}", "message": "PR is ready"},
        )
        assert "@eric" in _text(result)
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].body == "PR is ready"

    async def test_send_with_at_prefix(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        await biff_client.call_tool(
            "write",
            {"to": f"@eric:{_ERIC_TTY}", "message": "hello"},
        )
        unread = await state.relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].to_user == f"eric:{_ERIC_TTY}"


class TestCheckMessagesProtocol:
    async def test_no_messages(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("read_messages", {})
        assert "No new messages" in _text(result)

    async def test_receives_and_marks_read(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        from biff.models import Message

        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=state.session_key,
                body="check this out",
            )
        )
        result = await biff_client.call_tool("read_messages", {})
        text = _text(result)
        assert "eric" in text
        assert "check this out" in text

        # Second call shows no new messages
        result = await biff_client.call_tool("read_messages", {})
        assert "No new messages" in _text(result)


class TestDynamicDescriptionProtocol:
    """Verify check_messages description updates through MCP protocol."""

    async def _get_check_description(self, client: Client[Any]) -> str:
        tools = await client.list_tools()
        for tool in tools:
            if tool.name == "read_messages":
                assert tool.description is not None
                return tool.description
        raise AssertionError("read_messages tool not found")

    async def test_default_description(self, biff_client: Client[Any]) -> None:
        desc = await self._get_check_description(biff_client)
        assert "Check your inbox" in desc

    async def test_shows_unread_after_tool_call(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        from biff.models import Message

        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=state.session_key,
                body="auth module ready",
            )
        )
        # Call any tool to trigger refresh
        await biff_client.call_tool("plan", {"message": "working"})
        desc = await self._get_check_description(biff_client)
        assert "1 unread" in desc
        assert "@eric" in desc

    async def test_reverts_after_check(
        self, biff_client: Client[Any], state: ServerState
    ) -> None:
        from biff.models import Message

        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=state.session_key,
                body="hello",
            )
        )
        await biff_client.call_tool("plan", {"message": "working"})
        desc = await self._get_check_description(biff_client)
        assert "1 unread" in desc
        # Check messages clears unread
        await biff_client.call_tool("read_messages", {})
        desc = await self._get_check_description(biff_client)
        assert "Check your inbox" in desc
        assert "unread" not in desc

    async def test_fires_tool_list_changed_notification(
        self,
        tracked_client: tuple[Client[Any], _NotificationTracker],
        state: ServerState,
    ) -> None:
        """Tool call that changes the description sends list_changed."""
        from biff.models import Message

        client, tracker = tracked_client
        assert tracker.tool_list_changed_count == 0
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=state.session_key,
                body="PR ready",
            )
        )
        # Calling any tool triggers refresh, which should fire notification
        await client.call_tool("who", {})
        assert tracker.tool_list_changed_count >= 1

    async def test_no_notification_when_description_unchanged(
        self,
        tracked_client: tuple[Client[Any], _NotificationTracker],
    ) -> None:
        """Tool call with no description change skips notification."""
        client, tracker = tracked_client
        # First call may fire a notification (initial refresh);
        # capture baseline.
        await client.call_tool("who", {})
        before = tracker.tool_list_changed_count
        # Second call — no messages, description stays the same
        await client.call_tool("who", {})
        assert tracker.tool_list_changed_count == before
