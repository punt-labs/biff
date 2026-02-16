"""Tests for biff doctor diagnostics."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from biff.doctor import (
    CheckResult,
    _check_biff_file,
    _check_gh_cli,
    _check_mcp_server,
    _check_plugin_installed,
    _check_relay,
    _check_statusline,
    _print_check,
    _resolve_relay_config,
    check_environment,
)

# -- Individual checks -------------------------------------------------------


class TestCheckGhCli:
    @patch("biff.doctor.shutil.which", return_value=None)
    def test_not_installed(self, _mock: object) -> None:
        result = _check_gh_cli()
        assert not result.passed
        assert "not found" in result.message

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/gh")
    def test_not_authenticated(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 1  # type: ignore[attr-defined]
        result = _check_gh_cli()
        assert not result.passed
        assert "not authenticated" in result.message

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/gh")
    def test_authenticated(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        result = _check_gh_cli()
        assert result.passed


class TestCheckMcpServer:
    @patch("biff.doctor.shutil.which", return_value=None)
    def test_no_claude_cli(self, _mock: object) -> None:
        result = _check_mcp_server()
        assert not result.passed

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/claude")
    def test_biff_registered(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        mock_run.return_value.stdout = "biff: /usr/local/bin/biff serve"  # type: ignore[attr-defined]
        result = _check_mcp_server()
        assert result.passed

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/claude")
    def test_biff_not_registered(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        mock_run.return_value.stdout = "other-server: /usr/local/bin/other"  # type: ignore[attr-defined]
        result = _check_mcp_server()
        assert not result.passed


class TestCheckPluginInstalled:
    def test_commands_present(self, tmp_path: Path) -> None:
        commands = tmp_path / "commands"
        commands.mkdir()
        (commands / "write.md").write_text("test")
        (commands / "read.md").write_text("test")
        result = _check_plugin_installed(tmp_path)
        assert result.passed
        assert "2 commands" in result.message

    def test_no_commands_dir(self, tmp_path: Path) -> None:
        result = _check_plugin_installed(tmp_path)
        assert not result.passed


class TestCheckBiffFile:
    @patch("biff.doctor.find_git_root", return_value=None)
    def test_no_git_repo(self, _mock: object) -> None:
        result = _check_biff_file()
        assert not result.passed
        assert not result.required

    def test_file_exists(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("[team]\nmembers = []\n")
        with patch("biff.doctor.find_git_root", return_value=tmp_path):
            result = _check_biff_file()
        assert result.passed

    def test_file_missing(self, tmp_path: Path) -> None:
        with patch("biff.doctor.find_git_root", return_value=tmp_path):
            result = _check_biff_file()
        assert not result.passed
        assert not result.required


class TestCheckStatusline:
    def test_configured(self, tmp_path: Path) -> None:
        stash = tmp_path / "stash.json"
        stash.write_text("{}")
        with patch("biff.doctor.STASH_PATH", stash):
            result = _check_statusline()
        assert result.passed
        assert not result.required

    def test_not_configured(self, tmp_path: Path) -> None:
        with patch("biff.doctor.STASH_PATH", tmp_path / "nope"):
            result = _check_statusline()
        assert not result.passed
        assert not result.required


class TestResolveRelayConfig:
    @patch("biff.doctor.find_git_root", return_value=None)
    def test_defaults_to_demo_relay(self, _mock: object) -> None:
        url, auth = _resolve_relay_config()
        assert "ngs.global" in url
        assert auth is not None

    def test_uses_biff_file(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text(
            '[relay]\nurl = "nats://custom:4222"\ntoken = "s3cret"\n'
        )
        with patch("biff.doctor.find_git_root", return_value=tmp_path):
            url, auth = _resolve_relay_config()
        assert url == "nats://custom:4222"
        assert auth is not None
        assert auth.token == "s3cret"


class TestCheckRelay:
    @patch("biff.doctor._test_nats_connection", return_value=True)
    @patch("biff.doctor._resolve_relay_config")
    def test_reachable(self, mock_config: object, _mock_conn: object) -> None:
        mock_config.return_value = ("nats://localhost:4222", None)  # type: ignore[attr-defined]
        result = _check_relay()
        assert result.passed

    @patch("biff.doctor._test_nats_connection", return_value=False)
    @patch("biff.doctor._resolve_relay_config")
    def test_unreachable(self, mock_config: object, _mock_conn: object) -> None:
        mock_config.return_value = ("nats://localhost:4222", None)  # type: ignore[attr-defined]
        result = _check_relay()
        assert not result.passed

    @patch("biff.doctor.asyncio.run", side_effect=Exception("boom"))
    @patch("biff.doctor._resolve_relay_config")
    def test_connection_error(self, mock_config: object, _mock_run: object) -> None:
        mock_config.return_value = ("nats://localhost:4222", None)  # type: ignore[attr-defined]
        result = _check_relay()
        assert not result.passed
        assert "connection error" in result.message


# -- Output ------------------------------------------------------------------


class TestPrintCheck:
    def test_passed_symbol(self, capsys: object) -> None:
        _print_check(CheckResult("test", True, "ok"))
        assert "\u2713" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_failed_required_symbol(self, capsys: object) -> None:
        _print_check(CheckResult("test", False, "bad", required=True))
        assert "\u2717" in capsys.readouterr().out  # type: ignore[attr-defined]

    def test_failed_optional_symbol(self, capsys: object) -> None:
        _print_check(CheckResult("test", False, "skip", required=False))
        assert "\u25cb" in capsys.readouterr().out  # type: ignore[attr-defined]


# -- Aggregator --------------------------------------------------------------


class TestCheckEnvironment:
    @patch("biff.doctor._check_statusline")
    @patch("biff.doctor._check_biff_file")
    @patch("biff.doctor._check_relay")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_mcp_server")
    @patch("biff.doctor._check_gh_cli")
    def test_all_pass_returns_zero(
        self,
        mock_gh: object,
        mock_mcp: object,
        mock_plugin: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        for mock in [mock_gh, mock_mcp, mock_plugin, mock_relay]:
            mock.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        for mock in [mock_biff, mock_sl]:
            mock.return_value = CheckResult("test", True, "ok", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 0

    @patch("biff.doctor._check_statusline")
    @patch("biff.doctor._check_biff_file")
    @patch("biff.doctor._check_relay")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_mcp_server")
    @patch("biff.doctor._check_gh_cli")
    def test_required_failure_returns_one(
        self,
        mock_gh: object,
        mock_mcp: object,
        mock_plugin: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        mock_gh.return_value = CheckResult("gh", False, "missing")  # type: ignore[attr-defined]
        for mock in [mock_mcp, mock_plugin, mock_relay]:
            mock.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        for mock in [mock_biff, mock_sl]:
            mock.return_value = CheckResult("test", True, "ok", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 1

    @patch("biff.doctor._check_statusline")
    @patch("biff.doctor._check_biff_file")
    @patch("biff.doctor._check_relay")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_mcp_server")
    @patch("biff.doctor._check_gh_cli")
    def test_optional_failure_still_passes(
        self,
        mock_gh: object,
        mock_mcp: object,
        mock_plugin: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        for mock in [mock_gh, mock_mcp, mock_plugin, mock_relay]:
            mock.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        mock_biff.return_value = CheckResult(".biff", False, "missing", required=False)  # type: ignore[attr-defined]
        mock_sl.return_value = CheckResult("sl", False, "missing", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 0
