"""The first identified call binds the server and registers that session."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport

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


async def test_a_changed_session_id_repoints_the_server(
    state: ServerState, tmp_path: Path
) -> None:
    """/clear changes session_id in place on the long-lived stdio server."""
    unread = tmp_path / "unread"
    old, new = SessionRouting("sess-1"), SessionRouting("sess-2")
    before = len(asyncio.all_tasks())
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-1")
    ) as first:
        await first.call_tool("who", {})
        running = len(asyncio.all_tasks())
        assert old.unread_path(unread).exists()

        # The same long-lived server, after /clear: the hook now stamps sess-2.
        result = await first.call_tool("who", {"_caller": caller_for("sess-2")})
        assert "kai" in str(result)
        assert state.bound_session_id == "sess-2"
        assert state.session_key == new.session_key("kai")
        assert new.unread_path(unread).exists()
        assert not old.unread_path(unread).exists()
        assert await state.relay.get_session(old.session_key("kai")) is None
        assert await state.relay.get_session(new.session_key("kai")) is not None
    assert len(asyncio.all_tasks()) <= max(before, running)
