"""A server started under a declared session is present before any tool call."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig, Message
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.session_routing import SessionRouting
from biff.testing.caller import StampedClient, caller_for


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name="_test-startup", poll_interval=0.05),
        tmp_path / "data",
        hostname="h",
        pwd="/p",
        unread_dir=tmp_path / "unread",
    )


async def _until(predicate: object, timeout: float = 5.0) -> None:
    assert callable(predicate)
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.02)


async def test_env_session_registers_and_polls_with_no_tool_call(
    state: ServerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    routing = SessionRouting("env-sess")
    unread = routing.unread_path(tmp_path / "unread")
    async with StampedClient(FastMCPTransport(create_server(state))):
        assert state.bound_session_id == "env-sess"
        assert await state.relay.get_session(routing.session_key("kai")) is not None
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=routing.session_key("kai"),
                body="hello",
            )
        )
        await _until(
            lambda: unread.exists() and json.loads(unread.read_text())["count"] == 1
        )


async def test_a_stamped_call_for_the_same_session_does_not_register_again(
    state: ServerState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("env-sess")
    ) as client:
        before = len(asyncio.all_tasks())
        await client.call_tool("who", {})
        assert len(asyncio.all_tasks()) == before


async def test_a_stamped_call_for_another_session_repoints_from_the_env_one(
    state: ServerState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "env-sess")
    async with StampedClient(
        FastMCPTransport(create_server(state)), caller=caller_for("cleared")
    ) as client:
        await client.call_tool("who", {})
        assert state.bound_session_id == "cleared"
        old = SessionRouting("env-sess")
        assert not old.unread_path(tmp_path / "unread").exists()
        assert await state.relay.get_session(old.session_key("kai")) is None


@pytest.mark.parametrize("value", [None, "", "../evil", "a" * 129])
async def test_absent_or_malformed_env_session_stays_unbound(
    state: ServerState, monkeypatch: pytest.MonkeyPatch, value: str | None
) -> None:
    if value is None:
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    else:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", value)
    async with StampedClient(FastMCPTransport(create_server(state))):
        assert not state.is_bound
        assert await state.relay.get_sessions() == []
