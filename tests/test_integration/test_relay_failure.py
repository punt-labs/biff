"""Relay outages must not prevent MCP discovery."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.client.transports import FastMCPTransport
from mcp.shared.exceptions import MCPError

from biff.caller_identity import CallerIdentity
from biff.models import BiffConfig, RelayConnectError
from biff.server.app import create_server
from biff.testing.caller import DEFAULT_CALLER, StampedClient
from tests._state import create_state


@pytest.mark.parametrize("declared", [False, True])
@pytest.mark.parametrize("explicit_user", [False, True])
async def test_relay_failure_allows_discovery_and_reports_tool_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared: bool,
    explicit_user: bool,
) -> None:
    if declared:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "test-session")
    state = create_state(
        BiffConfig(
            user="kai",
            repo_name="test",
            relay_url="nats://localhost:4222",
            orgs=("test-org",),
        ),
        tmp_path,
    )
    failure = AsyncMock(side_effect=RelayConnectError("failed to connect to relay"))
    default_caller = CallerIdentity("fixed-startup", "kai") if explicit_user else None
    server = create_server(state, default_caller=default_caller)
    with patch("biff.nats_relay.NatsRelay._dial", failure):
        async with asyncio.timeout(2):
            async with StampedClient(
                FastMCPTransport(server),
                caller=None if explicit_user else DEFAULT_CALLER,
                mode="legacy",
            ) as client:
                assert client.initialize_result is not None
                assert await client.list_tools()
                with pytest.raises(MCPError, match="Relay connection failed"):
                    await client.call_tool("who", {})
                if explicit_user:
                    assert state.bound_session_id == "fixed-startup"
