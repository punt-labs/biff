"""Transport-level tests over real stdio subprocesses.

Verifies the wire protocol: CLI starts, JSON-RPC over stdin/stdout works,
and multiple subprocesses share state through the filesystem.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from mcp.types import TextContent

if TYPE_CHECKING:
    from fastmcp import Client
    from fastmcp.client.client import CallToolResult

pytestmark = pytest.mark.subprocess


def _text(result: CallToolResult) -> str:
    """Extract text from the first content block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


class TestServerStartup:
    """The biff subprocess starts and responds to protocol requests."""

    async def test_tools_list(self, biff_client: Client[Any]) -> None:
        """Server lists all tools over stdio transport."""
        tools = await biff_client.list_tools()
        names = {t.name for t in tools}
        expected = {"biff", "check_messages", "finger", "send_message", "who", "plan"}
        assert names == expected

    async def test_tools_have_descriptions(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.description, f"{tool.name} has no description"

    async def test_tools_have_input_schemas(self, biff_client: Client[Any]) -> None:
        tools = await biff_client.list_tools()
        for tool in tools:
            assert tool.inputSchema is not None, f"{tool.name} has no schema"


class TestToolCallOverStdio:
    """Tool calls round-trip correctly over the stdio wire protocol."""

    async def test_plan_returns_text(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("plan", {"message": "testing stdio"})
        assert "testing stdio" in _text(result)

    async def test_who_returns_text(self, biff_client: Client[Any]) -> None:
        await biff_client.call_tool("plan", {"message": "online"})
        result = await biff_client.call_tool("who", {})
        assert "@kai" in _text(result)

    async def test_finger_unknown_user(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("finger", {"user": "nobody"})
        assert "no active session" in _text(result)

    async def test_biff_toggle(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("biff", {"enabled": False})
        assert "off" in _text(result)
        result = await biff_client.call_tool("biff", {"enabled": True})
        assert "on" in _text(result)

    async def test_send_message_returns_text(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool(
            "send_message", {"to": "eric", "message": "hello over stdio"}
        )
        assert "@eric" in _text(result)

    async def test_check_messages_empty(self, biff_client: Client[Any]) -> None:
        result = await biff_client.call_tool("check_messages", {})
        assert "No new messages" in _text(result)


class TestCrossProcessState:
    """Two subprocesses share state through the same data directory."""

    async def test_plan_visible_across_processes(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
        shared_data_dir: Path,
    ) -> None:
        """kai's plan is visible to eric via separate subprocess."""
        await kai_client.call_tool("plan", {"message": "cross-process test"})
        result = await eric_client.call_tool("who", {})
        text = _text(result)
        assert "@kai" in text
        assert "cross-process test" in text

    async def test_both_visible_in_who(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """Both subprocess users appear in /who."""
        await kai_client.call_tool("plan", {"message": "kai working"})
        await eric_client.call_tool("plan", {"message": "eric working"})

        result = await kai_client.call_tool("who", {})
        text = _text(result)
        assert "@kai" in text
        assert "@eric" in text

    async def test_message_across_processes(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """kai sends a message; eric receives it across processes."""
        await kai_client.call_tool(
            "send_message", {"to": "eric", "message": "cross-process msg"}
        )
        result = await eric_client.call_tool("check_messages", {})
        text = _text(result)
        assert "@kai" in text
        assert "cross-process msg" in text


class TestDynamicDescriptionOverStdio:
    """check_messages description updates via stdio transport."""

    @staticmethod
    async def _check_desc(client: Client[Any]) -> str:
        tools = await client.list_tools()
        for t in tools:
            if t.name == "check_messages":
                assert t.description is not None
                return t.description
        raise AssertionError("check_messages not found")

    async def test_description_updates_after_message(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """kai sends to eric; eric's description shows unread."""
        await kai_client.call_tool(
            "send_message", {"to": "eric", "message": "subprocess test"}
        )
        # eric calls any tool to trigger refresh
        await eric_client.call_tool("plan", {"message": "working"})
        desc = await self._check_desc(eric_client)
        assert "1 unread" in desc
        assert "@kai" in desc

    async def test_description_reverts_after_check(
        self,
        kai_client: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        """After checking, description reverts to base."""
        await kai_client.call_tool("send_message", {"to": "eric", "message": "hello"})
        await eric_client.call_tool("plan", {"message": "working"})
        desc = await self._check_desc(eric_client)
        assert "1 unread" in desc
        # Check clears it
        await eric_client.call_tool("check_messages", {})
        desc = await self._check_desc(eric_client)
        assert "unread" not in desc
