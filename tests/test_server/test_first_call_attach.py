"""The first identified call binds the server and registers that session."""

from __future__ import annotations

import asyncio
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


async def test_repoint_waits_for_an_in_flight_call_of_the_old_session(
    state: ServerState,
) -> None:
    """A different-session call must not re-point under a running tool."""
    mcp = create_server(state)
    started, release = asyncio.Event(), asyncio.Event()
    seen: list[tuple[str | None, str | None]] = []

    @mcp.tool
    async def slow() -> str:
        seen.append((state.bound_session_id, None))
        started.set()
        await release.wait()
        seen.append((state.bound_session_id, state.session_key))
        return "done"

    async with StampedClient(
        FastMCPTransport(mcp), caller=caller_for("sess-1")
    ) as client:
        await client.call_tool("who", {})
        first = asyncio.create_task(client.call_tool("slow", {}))
        await started.wait()
        second = asyncio.create_task(
            client.call_tool("who", {"_caller": caller_for("sess-2")})
        )
        await asyncio.sleep(0.3)
        assert state.bound_session_id == "sess-1", "re-pointed under a running tool"
        release.set()
        await first
        await second
    assert seen[-1] == ("sess-1", SessionRouting("sess-1").session_key("kai"))
    assert state.bound_session_id == "sess-2"


async def test_a_failed_attach_is_logged_with_the_session(
    state: ServerState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("relay down")

    monkeypatch.setattr("biff.server.app._reap_sentinels", boom)
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-x")
    ) as client:
        with caplog.at_level("ERROR", logger="biff.server.app"):
            with pytest.raises(ToolError, match="relay down"):
                await client.call_tool("who", {})
    assert any(
        "sess-x" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records
    )
