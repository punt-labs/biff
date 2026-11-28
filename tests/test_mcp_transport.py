"""``mcp.transport`` picks how ``biff mcp`` reaches the relay: itself, or biffd."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from biff._cli_runtime import CliInvocation
from biff.mcp_transport import McpTransport
from biff.repo_transport import RepoTransport
from biff.server.entrypoints import McpServerOptions
from biff.server.mcp_assembly import McpAssembly

if TYPE_CHECKING:
    from pathlib import Path


class TestFromSettings:
    def test_the_default_is_biffd(self) -> None:
        assert McpTransport.from_settings({}, {}) is McpTransport.DAEMON

    def test_a_section_without_a_transport_key_is_biffd(self) -> None:
        assert McpTransport.from_settings({"mcp": {}}, {}) is McpTransport.DAEMON

    def test_the_config_key_can_still_choose_the_in_process_server(self) -> None:
        raw = {"mcp": {"transport": "inprocess"}}

        assert McpTransport.from_settings(raw, {}) is McpTransport.INPROCESS

    def test_the_environment_wins_over_the_config_key(self) -> None:
        raw = {"mcp": {"transport": "daemon"}}
        environ = {"BIFF_MCP_TRANSPORT": "inprocess"}

        assert McpTransport.from_settings(raw, environ) is McpTransport.INPROCESS

    def test_an_empty_environment_value_is_absent(self) -> None:
        raw = {"mcp": {"transport": "inprocess"}}

        assert McpTransport.from_settings(raw, {"BIFF_MCP_TRANSPORT": ""}) is (
            McpTransport.INPROCESS
        )

    @pytest.mark.parametrize(
        ("raw", "environ", "where"),
        [
            ({"mcp": {"transport": "deamon"}}, {}, "mcp.transport"),
            ({"mcp": {"transport": 1}}, {}, "mcp.transport"),
            ({"mcp": "daemon"}, {}, "mcp"),
            ({}, {"BIFF_MCP_TRANSPORT": "http"}, "BIFF_MCP_TRANSPORT"),
        ],
    )
    def test_a_value_that_names_neither_transport_is_refused_by_name(
        self, raw: dict[str, object], environ: dict[str, str], where: str
    ) -> None:
        with pytest.raises(SystemExit, match=where):
            McpTransport.from_settings(raw, environ)


class TestFromTheRepo:
    @staticmethod
    def _repo(tmp_path: Path, config: str) -> Path:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)  # noqa: S603, S607
        (tmp_path / ".punt-labs" / "biff").mkdir(parents=True)
        (tmp_path / ".punt-labs" / "biff" / "config.yaml").write_text(config)
        return tmp_path

    def test_a_repo_that_opts_out_gets_the_in_process_server(
        self, tmp_path: Path
    ) -> None:
        repo = self._repo(tmp_path, "mcp:\n  transport: inprocess\n")

        assert RepoTransport.of(repo, {}) is McpTransport.INPROCESS

    def test_a_repo_that_does_not_set_it_gets_biffd(self, tmp_path: Path) -> None:
        repo = self._repo(tmp_path, "team: []\n")

        assert RepoTransport.of(repo, {}) is McpTransport.DAEMON


class TestWhereTheDaemonIsUsed:
    @pytest.mark.parametrize(
        ("transport", "setting", "expected"),
        [
            ("stdio", "daemon", True),
            ("stdio", "inprocess", False),
            ("http", "daemon", False),
        ],
    )
    def test_only_the_stdio_server_asks_biffd(
        self, transport: str, setting: str, expected: bool
    ) -> None:
        environ = {"BIFF_MCP_TRANSPORT": setting}

        assert McpAssembly.uses_biffd(transport, None, environ) is expected

    def test_the_daemon_is_refused_at_launch_when_the_environment_carries_the_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # biffd cannot carry the override, so every channel subscription would fail
        # and the model would be told not to poll: refuse where it is seen.
        monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
        monkeypatch.setenv("BIFF_RELAY_URL", "nats://elsewhere:4222")
        options = McpServerOptions(user="rmh", data_dir=None, relay_url=None)

        with pytest.raises(SystemExit, match="BIFF_RELAY_URL") as caught:
            options.create_server(CliInvocation.repo_toggle())

        assert "BIFF_MCP_TRANSPORT=inprocess" in str(caught.value)

    def test_the_in_process_server_still_accepts_the_relay_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_MCP_TRANSPORT", "inprocess")
        monkeypatch.setenv("BIFF_RELAY_URL", "")
        options = McpServerOptions(user="rmh", data_dir=tmp_path, relay_url=None)

        options.create_server(CliInvocation.repo_toggle())  # does not raise

    def test_the_daemon_is_refused_when_an_option_moves_the_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_MCP_TRANSPORT", "daemon")
        options = McpServerOptions(user="rmh", data_dir=tmp_path, relay_url=None)

        with pytest.raises(SystemExit, match="--data-dir"):
            options.create_server(CliInvocation.repo_toggle())
