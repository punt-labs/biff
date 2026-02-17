"""TTY session integration tests.

Proves core properties of multi-session support through the MCP
protocol path:

1. Two sessions of the same user coexist in /who
2. /write @user delivers to user mailbox (POP, first reader consumes)
3. /write @user:tty delivers to one session (unicast)
4. /read merges user and TTY inboxes
5. Broadcasts persist when user has no sessions
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


class TestSessionLogout:
    """Server shutdown removes the session from shared storage."""

    async def test_session_removed_on_server_exit(
        self,
        kai_tty1_state: ServerState,
        eric_state: ServerState,
    ) -> None:
        # Eric's server stays up the whole time
        eric_mcp = create_server(eric_state)
        async with Client(FastMCPTransport(eric_mcp)) as eric:
            # Kai's server starts, registers, then exits
            kai_mcp = create_server(kai_tty1_state)
            async with Client(FastMCPTransport(kai_mcp)) as kai:
                await kai.call_tool("plan", {"message": "working"})
                # Eric sees Kai
                result = _text(await eric.call_tool("who", {}))
                assert "@kai" in result

            # Kai's server has exited — lifespan finally block ran
            result = _text(await eric.call_tool("who", {}))
            assert "@kai" not in result


class TestSentinelLogout:
    """Sentinel file removes a session even after concurrent writes."""

    async def test_sentinel_survives_heartbeat_race(
        self,
        kai_tty1_state: ServerState,
        eric_state: ServerState,
        shared_dir: Path,
    ) -> None:
        # Both servers up
        eric_mcp = create_server(eric_state)
        kai_mcp = create_server(kai_tty1_state)
        async with (
            Client(FastMCPTransport(eric_mcp)) as eric,
            Client(FastMCPTransport(kai_mcp)) as kai,
        ):
            await kai.call_tool("plan", {"message": "working"})
            assert "@kai" in _text(await eric.call_tool("who", {}))

            # Simulate signal handler: write sentinel for kai's session
            from biff.relay import LocalRelay

            assert isinstance(kai_tty1_state.relay, LocalRelay)
            kai_tty1_state.relay.write_remove_sentinel(kai_tty1_state.session_key)

            # Eric heartbeats (simulates concurrent write race)
            await eric.call_tool("plan", {"message": "still here"})

            # Eric's /who should NOT show kai — sentinel was reaped
            result = _text(await eric.call_tool("who", {}))
            assert "@kai" not in result
            assert "@eric" in result


class TestMulticastDelivery:
    """/write @user delivers to user mailbox — first reader consumes (POP)."""

    async def test_first_reader_consumes_broadcast(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        # Register kai sessions
        await kai_tty1.call_tool("plan", {"message": "working"})
        await kai_tty2.call_tool("plan", {"message": "also working"})
        # Eric sends to @kai (broadcast → user mailbox)
        await eric_client.call_tool("write", {"to": "kai", "message": "PR ready"})
        # First reader gets the message
        r1 = await kai_tty1.call_tool("read_messages", {})
        assert "PR ready" in _text(r1)
        # Second reader sees nothing — POP semantics
        r2 = await kai_tty2.call_tool("read_messages", {})
        assert "No new messages" in _text(r2)


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
    """Targeted messages are per-TTY — reading one doesn't affect another."""

    async def test_read_isolation(
        self,
        kai_tty1: Client[Any],
        kai_tty2: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        # Register kai sessions
        await kai_tty1.call_tool("plan", {"message": "working"})
        await kai_tty2.call_tool("plan", {"message": "also working"})
        # Eric sends targeted messages to each session
        await eric_client.call_tool("write", {"to": "kai:tty1", "message": "for tty1"})
        await eric_client.call_tool("write", {"to": "kai:tty2", "message": "for tty2"})
        # Each session sees only its own message
        r1 = await kai_tty1.call_tool("read_messages", {})
        r2 = await kai_tty2.call_tool("read_messages", {})
        assert "for tty1" in _text(r1)
        assert "for tty2" not in _text(r1)
        assert "for tty2" in _text(r2)
        assert "for tty1" not in _text(r2)


class TestOfflineDelivery:
    """Broadcast persists when user has no active sessions."""

    async def test_broadcast_persists_offline(
        self,
        shared_dir: Path,
    ) -> None:
        # Eric sends to @kai when kai has no sessions
        eric_state = create_state(
            BiffConfig(user="eric", repo_name=_TEST_REPO),
            shared_dir,
            tty="tty3",
            hostname="host-b",
            pwd="/project/c",
        )
        eric_mcp = create_server(eric_state)
        async with Client(FastMCPTransport(eric_mcp)) as eric:
            await eric.call_tool("write", {"to": "kai", "message": "offline msg"})

        # Kai comes online later and reads the message
        kai_state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            shared_dir,
            tty="tty1",
            hostname="host-a",
            pwd="/project/a",
        )
        kai_mcp = create_server(kai_state)
        async with Client(FastMCPTransport(kai_mcp)) as kai:
            r = await kai.call_tool("read_messages", {})
            assert "offline msg" in _text(r)


class TestDualInboxMerge:
    """/read shows both broadcast and targeted messages."""

    async def test_read_merges_both_inboxes(
        self,
        kai_tty1: Client[Any],
        eric_client: Client[Any],
    ) -> None:
        await kai_tty1.call_tool("plan", {"message": "working"})
        # Eric sends a broadcast and a targeted message
        await eric_client.call_tool("write", {"to": "kai", "message": "broadcast msg"})
        await eric_client.call_tool(
            "write", {"to": "kai:tty1", "message": "targeted msg"}
        )
        # Kai's /read shows both
        r = await kai_tty1.call_tool("read_messages", {})
        text = _text(r)
        assert "broadcast msg" in text
        assert "targeted msg" in text
