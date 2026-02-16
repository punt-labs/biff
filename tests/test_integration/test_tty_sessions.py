"""TTY session integration tests.

Proves core properties of multi-session support through the MCP
protocol path:

1. Two sessions of the same user coexist in /who
2. /write @user delivers to all sessions (multicast)
3. /write @user:tty delivers to one session (unicast)
4. /read is per-TTY (isolated inboxes)
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport
from mcp.types import TextContent

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import ServerState, create_state

_TEST_REPO = "_test-integration"


def _text(result: Any) -> str:
    """Extract text from the first content block of a tool result."""
    block = result.content[0]
    assert isinstance(block, TextContent)
    return block.text


@pytest.fixture
def shared_dir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def kai_tty1_state(shared_dir: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        shared_dir,
        tty="tty1",
        hostname="host-a",
        pwd="/project/a",
    )


@pytest.fixture
def kai_tty2_state(shared_dir: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        shared_dir,
        tty="tty2",
        hostname="host-a",
        pwd="/project/b",
    )


@pytest.fixture
def eric_state(shared_dir: Path) -> ServerState:
    return create_state(
        BiffConfig(user="eric", repo_name=_TEST_REPO),
        shared_dir,
        tty="tty3",
        hostname="host-b",
        pwd="/project/c",
    )


@pytest.fixture
async def kai_tty1(
    kai_tty1_state: ServerState,
) -> AsyncIterator[Client[Any]]:
    mcp = create_server(kai_tty1_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def kai_tty2(
    kai_tty2_state: ServerState,
) -> AsyncIterator[Client[Any]]:
    mcp = create_server(kai_tty2_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


@pytest.fixture
async def eric_client(
    eric_state: ServerState,
) -> AsyncIterator[Client[Any]]:
    mcp = create_server(eric_state)
    async with Client(FastMCPTransport(mcp)) as client:
        yield client


class TestTTYSessionCoexistence:
    """Two sessions of the same user coexist in /who."""

    async def test_who_shows_both_ttys(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
    ) -> None:
        # Register both sessions via tool calls
        await kai_tty1.call_tool("plan", {"message": "session 1"})
        await kai_tty2.call_tool("plan", {"message": "session 2"})
        # Who from either session should show both
        result = await kai_tty1.call_tool("who", {})
        text = _text(result)
        assert "tty1" in text
        assert "tty2" in text
        assert "session 1" in text
        assert "session 2" in text


class TestMulticastDelivery:
    """/write @user delivers to all sessions of that user."""

    async def test_broadcast_reaches_both_sessions(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        # Register kai sessions so broadcast can find them
        await kai_tty1.call_tool("plan", {"message": "working"})
        await kai_tty2.call_tool("plan", {"message": "also working"})
        # Eric sends to @kai (broadcast)
        await eric_client.call_tool("write", {"to": "kai", "message": "PR ready"})
        # Both sessions should receive the message
        r1 = await kai_tty1.call_tool("read_messages", {})
        r2 = await kai_tty2.call_tool("read_messages", {})
        assert "PR ready" in _text(r1)
        assert "PR ready" in _text(r2)


class TestUnicastDelivery:
    """/write @user:tty delivers to one session only."""

    async def test_targeted_reaches_one_session(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        # Register kai sessions
        await kai_tty1.call_tool("plan", {"message": "working"})
        await kai_tty2.call_tool("plan", {"message": "also working"})
        # Eric sends to @kai:tty1 specifically
        await eric_client.call_tool(
            "write",
            {"to": "kai:tty1", "message": "for tty1 only"},
        )
        # Only tty1 should receive the message
        r1 = await kai_tty1.call_tool("read_messages", {})
        r2 = await kai_tty2.call_tool("read_messages", {})
        assert "for tty1 only" in _text(r1)
        assert "No new messages" in _text(r2)


class TestPerTTYIsolation:
    """/read is per-TTY — reading one inbox doesn't affect another."""

    async def test_read_isolation(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        # Register kai sessions
        await kai_tty1.call_tool("plan", {"message": "working"})
        await kai_tty2.call_tool("plan", {"message": "also working"})
        # Eric sends to both sessions (broadcast)
        await eric_client.call_tool("write", {"to": "kai", "message": "hello team"})
        # Read from tty1 only
        r1 = await kai_tty1.call_tool("read_messages", {})
        assert "hello team" in _text(r1)
        # tty2 should still have unread message
        r2 = await kai_tty2.call_tool("read_messages", {})
        assert "hello team" in _text(r2)
