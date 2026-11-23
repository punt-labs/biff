"""Unstamped harness calls share the presence declared at MCP startup."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from biff._cli_runtime import CliInvocation
from biff.config import ResolvedConfig
from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.entrypoints import McpServerOptions
from biff.server.state import ServerState
from biff.testing import RecordingClient, Transcript


@pytest.mark.parametrize("enabled", [False, True])
async def test_unstamped_tools_use_startup_presence_only_when_opted_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, enabled: bool
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "devin-startup")
    marker = tmp_path / ".punt-labs/biff/enabled"
    marker.parent.mkdir(parents=True)
    marker.touch()
    resolved = ResolvedConfig(
        config=BiffConfig(user="devin", repo_name="test-default-caller"),
        data_dir=tmp_path / "data",
        repo_root=tmp_path,
        repo_common_root=tmp_path,
    )
    with (
        patch("biff.server.entrypoints.load_mcp_config", return_value=resolved),
        patch(
            "biff.server.transport_runtime.create_server", wraps=create_server
        ) as factory,
    ):
        server = McpServerOptions(
            user="devin" if enabled else None,
            data_dir=tmp_path,
            relay_url="",
        ).create_server(CliInvocation.repo_toggle())
    state = cast("ServerState", factory.call_args.args[0])
    if enabled:
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "changed-after-startup")
    async with Client(server) as client:
        recorder = RecordingClient(client, Transcript(title="Unstamped Devin"))
        startup = await state.relay.get_sessions()
        assert len(startup) == 1
        assert startup[0].user == "devin"
        assert state.bound_session_id == "devin-startup"
        startup_key = state.session_key
        if enabled:
            assert "devin" in await recorder.call("who", _caller=None)
            assert "No new messages." in await recorder.call(
                "read_messages", _caller=None
            )
            await recorder.call("write", to="devin", message="round trip", _caller=None)
            assert "round trip" in await recorder.call("read_messages", _caller=None)
            assert "round trip" not in await recorder.call(
                "read_messages", _caller=None
            )
            assert state.session_key == startup_key
            sessions = await state.relay.get_sessions()
            assert len(sessions) == 1
            assert sessions[0].user == "devin"
        else:
            for tool, args in (
                ("who", {}),
                ("write", {"to": "devin", "message": "refused"}),
                ("read_messages", {}),
            ):
                with pytest.raises(ToolError, match="stamping hook"):
                    await recorder.call(tool, _caller=None, **args)
