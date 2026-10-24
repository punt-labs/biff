"""CLI opt-in and config identity wiring, without live Ethos or relay calls."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from biff.__main__ import _create_mcp_server, app
from biff.config import EthosIdentity, load_mcp_config
from biff.ethos_session import EthosSessionPin
from biff.statusline import UNREAD_DIR

SID = "ses_123ABC"


def test_pinned_config_never_falls_back(tmp_path: Path) -> None:
    identity = EthosIdentity("rmh", "Raymond", "agent")
    with (
        patch("biff.config.resolve_agent_identity_from_disk") as disk,
        patch("biff.config.get_github_identity") as github,
        patch("biff.config.get_os_user") as os_user,
        patch("biff.config.find_git_root", return_value=tmp_path),
    ):
        resolved = load_mcp_config(
            start=tmp_path,
            data_dir_override=tmp_path,
            relay_url_override="",
            pinned_identity=identity,
        )
    assert resolved.config.user == "rmh"
    assert resolved.config.display_name == "Raymond"
    assert resolved.config.kind == "agent"
    disk.assert_not_called()
    github.assert_not_called()
    os_user.assert_not_called()


def test_factory_pin_wiring(tmp_path: Path) -> None:
    pin = EthosSessionPin(SID, SID)
    with (
        patch.object(
            EthosSessionPin,
            "resolve_identity",
            return_value=EthosIdentity("rmh", "Raymond", "agent"),
        ),
        patch("biff.__main__.create_server") as server,
        patch("biff.session_id.SessionHint.resolve_routing_id") as claude,
        patch("biff.session_key.find_session_key") as pid_key,
    ):
        _create_mcp_server(
            user=None,
            data_dir=tmp_path,
            relay_url="",
            prefix=tmp_path,
            ethos_session=SID,
            ethos_agent_id=SID,
        )
    state = server.call_args.args[0]
    assert state.config.user == "rmh"
    assert state.config.display_name == "Raymond"
    assert state.config.kind == "agent"
    assert state.ethos_session_pin == pin
    assert not state.auto_companion
    assert state.companion is None
    assert state.tty == pin.routing_id
    assert state.unread_path == pin.unread_path(UNREAD_DIR)
    claude.assert_not_called()
    pid_key.assert_not_called()


@pytest.mark.parametrize(
    "flags",
    [
        ["--ethos-session", SID],
        ["--ethos-agent-id", SID],
        ["--ethos-session", SID, "--ethos-agent-id", "claude"],
        ["--ethos-session", SID, "--ethos-agent-id", SID, "--user", "jim"],
    ],
)
def test_invalid_cli_flags_fail_before_resolution(flags: list[str]) -> None:
    with patch.object(EthosSessionPin, "resolve_identity") as identity:
        result = CliRunner().invoke(app, ["mcp", *flags])
    assert result.exit_code != 0
    identity.assert_not_called()


def test_cli_explicit_flags_start_stdio() -> None:
    with patch("biff.__main__._create_mcp_server") as factory:
        result = CliRunner().invoke(
            app, ["mcp", "--ethos-session", SID, "--ethos-agent-id", SID]
        )
    assert result.exit_code == 0
    assert factory.call_args.kwargs["ethos_session"] == SID
    assert factory.call_args.kwargs["ethos_agent_id"] == SID
    factory.return_value.run.assert_called_once_with(transport="stdio")


def test_environment_alone_does_not_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ETHOS_SESSION", SID)
    monkeypatch.setenv("ETHOS_AGENT_ID", SID)
    with (
        patch.object(EthosSessionPin, "resolve_identity") as identity,
        patch("biff.__main__.create_server") as server,
        patch("biff.session_id.SessionHint.resolve_routing_id", return_value="abcd"),
        patch("biff.session_key.find_session_key", return_value="123"),
    ):
        _create_mcp_server(
            user="claude",
            data_dir=tmp_path,
            relay_url="",
            prefix=tmp_path,
        )
    state = server.call_args.args[0]
    assert state.ethos_session_pin is None
    assert state.auto_companion
    assert state.tty == "abcd"
    assert state.unread_path == UNREAD_DIR / "123.json"
    identity.assert_not_called()
