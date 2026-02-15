"""Tests for biff CLI entry point."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from biff.__main__ import app
from biff.config import ResolvedConfig
from biff.models import BiffConfig

runner = CliRunner()

_RESOLVED = ResolvedConfig(
    config=BiffConfig(user="kai"),
    data_dir=Path("/tmp/biff/myrepo"),
    repo_root=Path("/proj/myrepo"),
)


class TestVersionCommand:
    def test_prints_version(self) -> None:
        result = runner.invoke(app, ["version"])
        assert result.exit_code == 0
        assert "biff" in result.output


class TestServeCommand:
    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_stdio_transport(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["serve", "--user", "kai"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(transport="stdio")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_http_transport(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["serve", "--user", "kai", "--transport", "http"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(
            transport="http", host="127.0.0.1", port=8419
        )

    def test_invalid_transport_rejected(self) -> None:
        result = runner.invoke(app, ["serve", "--user", "kai", "--transport", "htp"])
        assert result.exit_code != 0

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_custom_host_port(
        self,
        _mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(
            app,
            [
                "serve",
                "--user",
                "kai",
                "--transport",
                "http",
                "--host",
                "192.168.1.1",
                "--port",
                "9000",
            ],
        )
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(
            transport="http", host="192.168.1.1", port=9000
        )

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_passes_user_override(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["serve", "--user", "kai"])
        mock_config.assert_called_once()
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["user_override"] == "kai"

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_passes_data_dir_override(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["serve", "--user", "kai", "--data-dir", "/custom/dir"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["data_dir_override"] == Path("/custom/dir")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_passes_prefix(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["serve", "--user", "kai", "--prefix", "/var/spool"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["prefix"] == Path("/var/spool")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    @patch("biff.__main__.load_config", return_value=_RESOLVED)
    def test_no_user_delegates_to_config(
        self,
        mock_config: MagicMock,
        _mock_state: MagicMock,
        mock_server: MagicMock,
    ) -> None:
        """When --user is omitted, load_config gets user_override=None."""
        mock_server.return_value = MagicMock()
        runner.invoke(app, ["serve"])
        call_kwargs = mock_config.call_args.kwargs
        assert call_kwargs["user_override"] is None


class TestInitCommand:
    @patch("biff.__main__._set_git_user")
    @patch("biff.__main__._resolve_github_user", return_value=None)
    @patch("biff.__main__.get_os_user", return_value="kai")
    @patch("biff.__main__.get_git_user", return_value=None)
    @patch("biff.__main__.find_git_root")
    def test_creates_biff_file(
        self,
        mock_root: MagicMock,
        _mock_git: MagicMock,
        _mock_os: MagicMock,
        _mock_gh: MagicMock,
        _mock_set: MagicMock,
        tmp_path: Path,
    ) -> None:
        mock_root.return_value = tmp_path
        # Simulate: confirm git config=yes, members="eric, priya", relay=""
        result = runner.invoke(app, ["init"], input="y\neric, priya\n\n")
        assert result.exit_code == 0
        biff_file = tmp_path / ".biff"
        assert biff_file.exists()
        content = biff_file.read_text()
        assert '"eric"' in content
        assert '"priya"' in content

    @patch("biff.__main__.find_git_root", return_value=None)
    def test_not_in_repo(self, _mock: MagicMock) -> None:
        result = runner.invoke(app, ["init"])
        assert result.exit_code != 0
        assert "Not in a git repository" in result.output

    @patch("biff.__main__.find_git_root")
    def test_already_exists(self, mock_root: MagicMock, tmp_path: Path) -> None:
        mock_root.return_value = tmp_path
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
        result = runner.invoke(app, ["init"])
        assert result.exit_code != 0
        assert "already exists" in result.output
