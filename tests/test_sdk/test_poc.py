"""Proof-of-concept: Claude Agent SDK drives biff via MCP.

Validates that the SDK can:
1. Launch a Claude session with biff as an MCP server
2. Claude discovers and calls biff tools
3. Tool results flow back through the session
4. Cross-user visibility works across two SDK sessions sharing a data dir
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from ._client import SDKClient

pytestmark = pytest.mark.sdk


class TestSDKCanCallBiffTools:
    """Verify the SDK can drive a Claude session that calls biff tools."""

    async def test_plan_tool_via_sdk(self, kai: SDKClient) -> None:
        """Claude sets a plan via the biff MCP server."""
        result = await kai.prompt(
            'Call the "plan" tool with message "testing SDK integration".'
        )

        assert result.tool_calls, "Expected at least one tool call"
        plans = [tc for tc in result.tool_calls if tc.name == "plan"]
        names = [tc.name for tc in result.tool_calls]
        assert plans, f"Expected plan call, got: {names}"
        assert plans[0].arguments.get("message") == "testing SDK integration"
        assert not result.is_error

    async def test_finger_tool_via_sdk(self, kai: SDKClient, eric: SDKClient) -> None:
        """Claude checks a user's status via the finger tool."""
        # Set kai's plan via direct subprocess (no API call)
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        transport = StdioTransport(
            command="uv",
            args=[
                "run",
                "biff",
                "serve",
                "--user",
                "kai",
                "--data-dir",
                str(kai.data_dir),
                "--transport",
                "stdio",
            ],
        )
        async with Client(transport) as client:
            await client.call_tool("plan", {"message": "working on SDK spike"})

        # SDK session: eric fingers kai
        result = await eric.prompt('Call the "finger" tool with user "@kai".')

        fingers = [tc for tc in result.tool_calls if tc.name == "finger"]
        names = [tc.name for tc in result.tool_calls]
        assert fingers, f"Expected finger call, got: {names}"
        assert "working on SDK spike" in result.tool_output


class TestCrossSessionVisibility:
    """Two SDK-driven sessions share state through the filesystem."""

    @pytest.mark.transcript
    async def test_plan_visible_across_sessions(
        self, kai: SDKClient, eric: SDKClient
    ) -> None:
        """kai sets plan in one session; eric sees it in another."""
        kai.transcript.title = "SDK: cross-session plan visibility"
        kai.transcript.description = (
            "Two Claude Code sessions share presence state via filesystem."
        )

        await kai.prompt('Call the "plan" tool with message "cross-session test".')

        result = await eric.prompt('Call the "who" tool to see who is online.')

        assert "kai" in result.tool_output
        assert "cross-session" in result.tool_output
