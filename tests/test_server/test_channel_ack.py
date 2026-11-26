"""The ``channel_ack`` tool: the model's half of a channel check (DES-087)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from fastmcp.tools.function_tool import FunctionTool

from biff.models import BiffConfig
from biff.nats_relay import NatsRelay
from biff.server.app import create_server
from biff.server.tools import register_channel_tools
from biff.unread import read_session_unread
from tests._state import create_state

if TYPE_CHECKING:
    from pathlib import Path

    from biff.server.state import ServerState

_REPLY = "_INBOX.doctor1"


class _RecordingRelay(NatsRelay):
    """A NATS relay that never dials; it records the acknowledgements it is given."""

    acks: list[tuple[str, str, str]]
    fails: bool = False

    async def publish_channel_check_ack(
        self, reply: str, session_key: str, token: str
    ) -> None:
        if self.fails:
            raise ConnectionError("relay down")
        self.acks = [*getattr(self, "acks", []), (reply, session_key, token)]


def _state(tmp_path: Path) -> ServerState:
    config = BiffConfig(user="kai", repo_name="_test-server")
    return create_state(
        config,
        tmp_path,
        tty="tty1",
        hostname="h",
        pwd="/p",
        repo_root=tmp_path,
        unread_path=tmp_path / "unread.json",
    )


async def _ack(state: ServerState, token: str) -> str:
    mcp = create_server(state)
    register_channel_tools(mcp, state)
    tool = await mcp.get_tool("channel_ack")
    assert isinstance(tool, FunctionTool)
    return str(await tool.fn(token=token))


def _request(state: ServerState) -> str:
    """Make the emitter accept a check and return its token."""
    token = state.channel_check.request(_REPLY)
    assert token is not None
    return token


class TestOnTheNatsRelay:
    @pytest.fixture
    def state(self, tmp_path: Path) -> ServerState:
        state = _state(tmp_path)
        relay = _RecordingRelay(url="nats://localhost:4222", stream_prefix="biff-dev")
        object.__setattr__(state, "relay", relay)
        return state

    async def test_the_right_token_publishes_to_the_reply_subject(
        self, state: ServerState
    ) -> None:
        token = _request(state)
        assert await _ack(state, token) == "biff channel live."
        assert isinstance(state.relay, _RecordingRelay)
        assert state.relay.acks == [(_REPLY, "kai:tty1", token)]

    async def test_a_wrong_token_publishes_nothing_and_says_so(
        self, state: ServerState
    ) -> None:
        _request(state)
        reply = await _ack(state, "0" * 32)
        assert "not accepted" in reply
        assert isinstance(state.relay, _RecordingRelay)
        assert getattr(state.relay, "acks", []) == []

    async def test_a_repeated_token_is_not_accepted(self, state: ServerState) -> None:
        token = _request(state)
        await _ack(state, token)
        assert "not accepted" in await _ack(state, token)
        assert isinstance(state.relay, _RecordingRelay)
        assert len(state.relay.acks) == 1

    async def test_a_relay_failure_is_reported_not_raised(
        self, state: ServerState
    ) -> None:
        token = _request(state)
        assert isinstance(state.relay, _RecordingRelay)
        state.relay.fails = True
        reply = await _ack(state, token)
        assert reply.startswith("biff channel live")
        assert "reply" in reply


class TestOnTheLocalRelay:
    async def test_a_match_writes_channel_checked_at_to_the_unread_file(
        self, tmp_path: Path
    ) -> None:
        state = _state(tmp_path)
        token = _request(state)
        assert await _ack(state, token) == "biff channel live."
        assert state.unread_path is not None
        unread = read_session_unread(state.unread_path)
        assert unread is not None
        assert unread.channel_checked_at is not None
        assert json.loads(state.unread_path.read_text())["channel_checked_at"] > 0

    async def test_a_wrong_token_writes_nothing(self, tmp_path: Path) -> None:
        state = _state(tmp_path)
        _request(state)
        await _ack(state, "0" * 32)
        assert state.unread_path is not None
        assert not state.unread_path.exists()
