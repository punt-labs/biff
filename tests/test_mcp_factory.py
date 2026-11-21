"""The MCP server factory leaves the session for the first call to declare."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from biff.__main__ import app
from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.entrypoints import McpServerOptions
from biff.server.state import create_state
from biff.statusline import UNREAD_DIR
from biff.testing.caller import StampedClient


def test_factory_creates_an_unbound_state_with_the_unread_directory(
    tmp_path: Path,
) -> None:
    with patch("biff.server.entrypoints.create_server") as server:
        McpServerOptions(
            user="rmh", data_dir=tmp_path, relay_url="", prefix=tmp_path
        ).create_server()
    state = server.call_args.args[0]
    assert state.is_bound is False
    assert state.unread_dir == UNREAD_DIR
    assert state.unread_path is None


def test_removed_pin_flags_are_rejected() -> None:
    for flag in ("--ethos-session", "--ethos-agent-id"):
        result = CliRunner().invoke(app, ["serve", flag, "ses_1"])
        assert result.exit_code != 0
        assert "No such option" in result.output


@pytest.mark.parametrize("mode", ["mcp", "serve"])
def test_session_setup_logs_no_identity(
    mode: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    state = create_state(
        BiffConfig(
            user="private-login", display_name="Private Person", repo_name="test"
        ),
        tmp_path,
    )
    server = create_server(state)

    async def exercise_session() -> None:
        async with StampedClient(server) as client:
            await client.call_tool("who")

    def run_session(**_kwargs: object) -> None:
        asyncio.run(exercise_session())

    with (
        caplog.at_level(logging.INFO, logger="biff"),
        patch.object(McpServerOptions, "_observed_server", return_value=server),
        patch.object(server, "run", side_effect=run_session) as run,
    ):
        ctx = typer.Context(typer.main.get_command(app))
        if mode == "mcp":
            McpServerOptions.mcp(ctx)
            run.assert_called_once_with(transport="stdio")
        else:
            McpServerOptions.serve(ctx)
            run.assert_called_once_with(transport="http", host="127.0.0.1", port=8419)

    messages = [r.getMessage() for r in caplog.records if r.name.startswith("biff")]
    assert any(m.startswith(f"Session ready: {state.tty} (") for m in messages)
    assert all("private-login" not in m and "Private Person" not in m for m in messages)
