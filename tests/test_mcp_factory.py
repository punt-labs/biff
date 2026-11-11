"""The MCP server factory leaves the session for the first call to declare."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from biff.__main__ import _create_mcp_server, app
from biff.statusline import UNREAD_DIR


def test_factory_creates_an_unbound_state_with_the_unread_directory(
    tmp_path: Path,
) -> None:
    with patch("biff.__main__.create_server") as server:
        _create_mcp_server(user="rmh", data_dir=tmp_path, relay_url="", prefix=tmp_path)
    state = server.call_args.args[0]
    assert state.is_bound is False
    assert state.unread_dir == UNREAD_DIR
    assert state.unread_path is None


def test_removed_pin_flags_are_rejected() -> None:
    for flag in ("--ethos-session", "--ethos-agent-id"):
        result = CliRunner().invoke(app, ["serve", flag, "ses_1"])
        assert result.exit_code != 0
        assert "No such option" in result.output
