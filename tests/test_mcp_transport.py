"""``mcp.transport`` picks how ``biff mcp`` reaches the relay: itself, or biffd."""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from biff.mcp_transport import McpTransport
from biff.server.mcp_assembly import McpAssembly

if TYPE_CHECKING:
    from pathlib import Path


class TestFromSettings:
    def test_the_default_is_the_in_process_server(self) -> None:
        assert McpTransport.from_settings({}, {}) is McpTransport.INPROCESS

    def test_the_config_key_selects_the_daemon(self) -> None:
        raw = {"mcp": {"transport": "daemon"}}

        assert McpTransport.from_settings(raw, {}) is McpTransport.DAEMON

    def test_the_environment_wins_over_the_config_key(self) -> None:
        raw = {"mcp": {"transport": "daemon"}}
        environ = {"BIFF_MCP_TRANSPORT": "inprocess"}

        assert McpTransport.from_settings(raw, environ) is McpTransport.INPROCESS

    def test_an_empty_environment_value_is_absent(self) -> None:
        raw = {"mcp": {"transport": "daemon"}}

        assert McpTransport.from_settings(raw, {"BIFF_MCP_TRANSPORT": ""}) is (
            McpTransport.DAEMON
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

    def test_a_repo_that_sets_the_key_gets_the_daemon_transport(
        self, tmp_path: Path
    ) -> None:
        repo = self._repo(tmp_path, "mcp:\n  transport: daemon\n")

        assert McpAssembly.transport_for(repo, {}) is McpTransport.DAEMON

    def test_a_repo_that_does_not_set_it_keeps_the_in_process_server(
        self, tmp_path: Path
    ) -> None:
        repo = self._repo(tmp_path, "team: []\n")

        assert McpAssembly.transport_for(repo, {}) is McpTransport.INPROCESS
