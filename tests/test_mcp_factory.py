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
from biff.caller_identity import CallerIdentity
from biff.models import BiffConfig
from biff.server.app import create_server
from biff.server.caller_policy import CallerPolicy
from biff.server.entrypoints import McpServerOptions
from biff.server.state import create_state
from biff.server.transport_runtime import McpLifecycleHandler
from biff.statusline import UNREAD_DIR
from biff.testing.caller import StampedClient


def test_factory_creates_an_unbound_state_with_the_unread_directory(
    tmp_path: Path,
) -> None:
    with patch("biff.server.transport_runtime.create_server") as server:
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


def test_default_caller_uses_startup_session_and_explicit_user(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "startup-session")
    with patch("biff.server.transport_runtime.create_server") as server:
        McpServerOptions(
            user="devin",
            data_dir=tmp_path,
            relay_url="",
            prefix=tmp_path,
        ).create_server()
    assert server.call_args.kwargs["default_caller"] == CallerIdentity(
        "startup-session", "devin"
    )


@pytest.mark.parametrize("session", ["../invalid", "invalid/session"])
def test_default_caller_requires_valid_startup_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, session: str
) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", session)
    with pytest.raises(typer.BadParameter, match="CLAUDE_CODE_SESSION_ID"):
        McpServerOptions(
            user="devin",
            data_dir=tmp_path,
            relay_url="",
            prefix=tmp_path,
        ).create_server()


def test_default_caller_has_no_new_flag() -> None:
    runner = CliRunner()
    with patch.object(McpServerOptions, "_observed_server") as observed:
        result = runner.invoke(app, ["mcp", "--user", "devin"])
    assert result.exit_code == 0
    observed.return_value.run.assert_called_once_with(transport="stdio")
    result = runner.invoke(app, ["serve", "--default-caller"])
    assert result.exit_code != 0
    assert "No such option" in result.output


@pytest.mark.parametrize("transport", ["stdio", "http"])
def test_only_explicit_stdio_user_enables_default_caller(
    tmp_path: Path, transport: str
) -> None:
    with patch("biff.server.transport_runtime.create_server") as server:
        options = McpServerOptions(
            user=None if transport == "stdio" else "devin",
            data_dir=tmp_path,
            relay_url="",
            prefix=tmp_path,
            transport="stdio" if transport == "stdio" else "http",
        )
        options.create_server()
    assert server.call_args.kwargs["default_caller"] is None


def test_explicit_user_gets_fresh_session_without_environment(tmp_path: Path) -> None:
    with patch("biff.server.transport_runtime.create_server") as server:
        options = McpServerOptions(
            user="devin", data_dir=tmp_path, relay_url="", prefix=tmp_path
        )
        options.create_server()
        first = server.call_args.kwargs["default_caller"]
        options.create_server()
        second = server.call_args.kwargs["default_caller"]
    assert isinstance(first, CallerIdentity) and isinstance(second, CallerIdentity)
    assert first.agent_id == second.agent_id == "devin"
    assert first.session_id != second.session_id


def test_inferred_default_user_does_not_enable_unstamped_calls(tmp_path: Path) -> None:
    with patch("biff.server.transport_runtime.create_server") as server:
        McpServerOptions(
            user=None, data_dir=tmp_path, relay_url="", prefix=tmp_path
        ).create_server(default_user="devin")
    assert server.call_args.kwargs["default_caller"] is None


@pytest.mark.parametrize("user", ["", "../invalid", "a" * 129])
def test_default_caller_rejects_invalid_explicit_users(user: str) -> None:
    with pytest.raises(typer.BadParameter, match="user handle"):
        CallerPolicy.for_launch(user, "stdio")


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
            run.assert_called_once_with(
                transport="http",
                host="127.0.0.1",
                port=8419,
                uvicorn_config={"log_config": McpLifecycleHandler.configuration()},
            )

    messages = [r.getMessage() for r in caplog.records if r.name.startswith("biff")]
    assert any(m.startswith(f"Session ready: {state.tty} (") for m in messages)
    assert all("private-login" not in m and "Private Person" not in m for m in messages)
    visible = [m for m in messages if m.startswith("Visible repos:")]
    assert visible == ["Visible repos: count=1"]
    assert all("test" not in m and str(tmp_path) not in m for m in visible)
