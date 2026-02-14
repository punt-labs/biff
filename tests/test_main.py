"""Tests for biff CLI entry point."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from biff.__main__ import app

runner = CliRunner()


class TestServeCommand:
    def test_requires_user_option(self) -> None:
        result = runner.invoke(app, [])
        assert result.exit_code != 0

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    def test_stdio_transport(
        self, mock_state: MagicMock, mock_server: MagicMock
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["--user", "kai"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(transport="stdio")

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    def test_http_transport(
        self, mock_state: MagicMock, mock_server: MagicMock
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(app, ["--user", "kai", "--transport", "http"])
        assert result.exit_code == 0
        mock_mcp.run.assert_called_once_with(
            transport="http", host="127.0.0.1", port=8419
        )

    def test_invalid_transport_rejected(self) -> None:
        result = runner.invoke(app, ["--user", "kai", "--transport", "htp"])
        assert result.exit_code != 0

    @patch("biff.__main__.create_server")
    @patch("biff.__main__.create_state")
    def test_custom_host_port(
        self, mock_state: MagicMock, mock_server: MagicMock
    ) -> None:
        mock_mcp = MagicMock()
        mock_server.return_value = mock_mcp
        result = runner.invoke(
            app,
            [
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
