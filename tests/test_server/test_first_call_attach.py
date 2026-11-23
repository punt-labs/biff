"""The first identified call binds the server and registers that session."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp.client.transports import FastMCPTransport
from fastmcp.exceptions import ToolError
from mcp.shared.exceptions import MCPError
from mcp.types import INTERNAL_ERROR

from biff.models import BiffConfig, RelayConnectError
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.poller_supervisor import reap_sentinels
from biff.server.state import ServerState
from biff.session_routing import SessionRouting
from biff.testing.caller import StampedClient, caller_for
from tests._state import create_state


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


@pytest.mark.parametrize("mode", ["2026-07-28", "legacy"])
async def test_a_failed_attach_is_logged_without_session_content(
    state: ServerState,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    mode: Literal["2026-07-28", "legacy"],
) -> None:
    async def boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("relay down")

    monkeypatch.setattr("biff.server.app.reap_sentinels", boom)
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-x"), mode=mode
    ) as client:
        with (
            caplog.at_level("ERROR", logger="biff.server.app"),
            pytest.raises(MCPError) as exc,
        ):
            await client.call_tool("who", {})
    assert exc.value.code == INTERNAL_ERROR
    assert exc.value.message == "Internal server error"
    assert exc.value.data is None
    assert any(
        r.getMessage().startswith(
            "MCP session registration failed: RuntimeError at registration line "
        )
        and r.levelname == "ERROR"
        for r in caplog.records
    )
    assert "sess-x" not in caplog.text
    assert "relay down" not in caplog.text


@pytest.mark.parametrize("stage", ["update_session", "after_registration"])
@pytest.mark.parametrize("failure", [RelayConnectError, TimeoutError])
async def test_failed_startup_rolls_back_and_can_retry(
    state: ServerState,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    failure: type[Exception],
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-retry")
    failed = AsyncMock(
        side_effect=failure("private relay detail"),
        wraps=state.relay.update_session
        if stage == "update_session"
        else reap_sentinels,
    )
    target = (
        patch.object(state.relay, "update_session", failed)
        if stage == "update_session"
        else patch("biff.server.app.reap_sentinels", failed)
    )
    with target:
        async with StampedClient(
            FastMCPTransport(create_server(state)), caller=caller_for("sess-retry")
        ) as client:
            assert await client.list_tools()
            assert await state.relay.get_sessions() == []
            assert await state.relay.list_reserved_names("kai") == []
            failed.side_effect = None
            await client.call_tool("who", {})
            assert len(await state.relay.get_sessions()) == 1


async def test_attached_tool_failure_reports_relay_error(state: ServerState) -> None:
    relay = NatsRelay("nats://localhost:4222")

    async def redial(_repos: object) -> None:
        await relay._dial()

    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("sess-tool")
    ) as client:
        await client.call_tool("who", {})
        with (
            patch(
                "nats.aio.client.Client.connect",
                AsyncMock(side_effect=OSError("private detail")),
            ),
            patch.object(
                state.relay,
                "get_sessions_for_repos",
                AsyncMock(side_effect=redial),
            ),
            pytest.raises(ToolError, match="Relay connection failed"),
        ):
            await client.call_tool("who", {})
