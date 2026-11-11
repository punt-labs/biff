"""The first identified call binds the server and registers that session."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError

from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.session_routing import SessionRouting
from biff.testing.caller import StampedClient, caller_for


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name="_test-attach"),
        tmp_path / "data",
        hostname="h",
        pwd="/p",
        unread_dir=tmp_path / "unread",
    )


async def test_nothing_is_registered_before_the_first_call(
    state: ServerState, tmp_path: Path
) -> None:
    async with StampedClient(FastMCPTransport(create_server(state))):
        assert not state.is_bound
        assert await state.relay.get_sessions() == []
        assert not (tmp_path / "unread").exists()


async def test_first_call_binds_registers_and_writes_the_unread_file(
    state: ServerState, tmp_path: Path
) -> None:
    routing = SessionRouting("sess-1")
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-1")
    ) as client:
        await client.call_tool("who", {})
        assert state.bound_session_id == "sess-1"
        assert state.session_key == routing.session_key("kai")
        assert await state.relay.get_session(routing.session_key("kai")) is not None
        assert routing.unread_path(tmp_path / "unread").exists()


async def test_a_different_session_is_refused(state: ServerState) -> None:
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-1")
    ) as client:
        await client.call_tool("who", {})
        other = StampedClient(
            FastMCPTransport(create_server(state)), caller=caller_for("sess-2")
        )
        async with other:
            with pytest.raises(ToolError, match="bound to session"):
                await other.call_tool("who", {})
