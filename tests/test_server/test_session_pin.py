"""Pinned callers cannot cross session boundaries or imply human liveness."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from biff.ethos_session import EthosSessionPin
from biff.models import BiffConfig
from biff.server.app import (
    _heartbeat_loop,
    _poll_companion_registration,
    create_server,
)
from biff.server.state import create_state

SID = "ses_123ABC"


@pytest.mark.parametrize(
    "meta",
    [
        None,
        {},
        {"sessionID": SID},
        {"ai.opencode/sessionID": "ses_other"},
        {"ai.opencode/sessionID": None},
        {"ai.opencode/sessionID": 123},
        {"ai.opencode/sessionID": {"id": SID}},
        {"ai.opencode/sessionID": [SID]},
    ],
)
async def test_invalid_caller_cannot_mutate_state(
    tmp_path: Path, meta: dict[str, object] | None
) -> None:
    pin = EthosSessionPin(SID, SID)
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path,
        dormant=True,
        ethos_session_pin=pin,
        auto_companion=False,
        tty=pin.routing_id,
    )
    with patch.object(type(state.activity), "touch") as touch:
        async with Client(create_server(state)) as client:
            with pytest.raises(ToolError, match="pinned"):
                await client.call_tool("plan", {"message": "wrong session"}, meta=meta)
    touch.assert_not_called()


async def test_matching_caller_and_legacy_no_metadata(tmp_path: Path) -> None:
    pin = EthosSessionPin(SID, SID)
    pinned = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path,
        dormant=True,
        ethos_session_pin=pin,
        auto_companion=False,
    )
    async with Client(create_server(pinned)) as client:
        result = await client.call_tool("who", meta={"ai.opencode/sessionID": SID})
        assert not result.is_error
    legacy = create_state(
        BiffConfig(user="claude", repo_name="test"), tmp_path, dormant=True
    )
    async with Client(create_server(legacy)) as client:
        result = await client.call_tool("who")
        assert not result.is_error


async def test_companion_disabled_even_after_heartbeat(tmp_path: Path) -> None:
    state = create_state(
        BiffConfig(user="rmh", repo_name="test"),
        tmp_path,
        ethos_session_pin=EthosSessionPin(SID, SID),
        auto_companion=False,
    )
    shutdown = asyncio.Event()

    async def heartbeat(_key: str) -> None:
        shutdown.set()

    with (
        patch("biff.config.get_ethos_roster") as roster,
        patch.object(type(state.relay), "heartbeat", side_effect=heartbeat) as beat,
        patch("biff.server.app._refresh_org_repos", new_callable=AsyncMock),
    ):
        await _poll_companion_registration(state)
        await _heartbeat_loop(state, shutdown, interval=0.001)
    beat.assert_called_once_with(state.session_key)
    roster.assert_not_called()
    assert state.companion is None
    assert not await state.relay.get_sessions()


def test_pin_requires_explicit_companion_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="companions"):
        create_state(
            BiffConfig(user="rmh", repo_name="test"),
            tmp_path,
            ethos_session_pin=EthosSessionPin(SID, SID),
        )


async def test_active_identity_unread_and_rejected_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    pin = EthosSessionPin(SID, SID)
    unread = pin.unread_path(tmp_path / "unread")
    state = create_state(
        BiffConfig(user="rmh", display_name="Raymond", kind="agent", repo_name="test"),
        tmp_path / "relay",
        tty=pin.routing_id,
        unread_path=unread,
        ethos_session_pin=pin,
        auto_companion=False,
    )
    with patch("biff.server.app.signal.signal"):
        async with Client(create_server(state)) as client:
            sessions = await state.relay.get_sessions()
            assert len(sessions) == 1
            assert sessions[0].user == "rmh"
            assert sessions[0].display_name == "Raymond"
            assert sessions[0].kind == "agent"
            assert unread.exists()
            status: object = json.loads(unread.read_text())
            assert isinstance(status, dict)
            assert status["user"] == "rmh"
            with pytest.raises(ToolError, match="pinned"):
                await client.call_tool(
                    "plan",
                    {"message": "wrong"},
                    meta={"ai.opencode/sessionID": "ses_other"},
                )
            unchanged = await state.relay.get_session(state.session_key)
            assert unchanged is not None and unchanged.plan == ""
            await client.call_tool(
                "plan", {"message": "right"}, meta={"ai.opencode/sessionID": SID}
            )
            changed = await state.relay.get_session(state.session_key)
            assert changed is not None and changed.plan == "right"
            assert state.companion is None
    assert not unread.exists()
