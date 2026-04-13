"""Tests for biff doctor diagnostics."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from biff.doctor import (
    BIFF_COMMANDS,
    CheckResult,
    _check_biff_file,
    _check_gh_cli,
    _check_plugin_installed,
    _check_relay,
    _check_statusline,
    _check_user_commands,
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
        assert not result.required
        assert "not found" in result.message

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/gh")
    def test_not_authenticated(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 1  # type: ignore[attr-defined]
        result = _check_gh_cli()
        assert not result.passed
        assert not result.required
        assert "not authenticated" in result.message

    @patch("biff.doctor.subprocess.run")
    @patch("biff.doctor.shutil.which", return_value="/usr/bin/gh")
    def test_authenticated(self, _which: object, mock_run: object) -> None:
        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        result = _check_gh_cli()
        assert result.passed


class TestCheckPluginInstalled:
    def test_installed(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"biff@punt-labs": [{"scope": "user"}]}})
        )
        with patch("biff.doctor.Path.home", return_value=tmp_path):
            result = _check_plugin_installed()
        assert result.passed

    def test_not_installed_no_file(self, tmp_path: Path) -> None:
        with patch("biff.doctor.Path.home", return_value=tmp_path):
            result = _check_plugin_installed()
        assert not result.passed

    def test_not_installed_missing_entry(self, tmp_path: Path) -> None:
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        (plugins_dir / "installed_plugins.json").write_text(
            json.dumps({"plugins": {"other@local": [{}]}})
        )
        with patch("biff.doctor.Path.home", return_value=tmp_path):
            result = _check_plugin_installed()
        assert not result.passed


class TestCheckUserCommands:
    def test_all_deployed(self, tmp_path: Path) -> None:
        for name in BIFF_COMMANDS:
            (tmp_path / name).write_text("test")
        result = _check_user_commands(tmp_path)
        assert result.passed
        assert not result.required

    def test_missing_when_absent(self, tmp_path: Path) -> None:
        result = _check_user_commands(tmp_path)
        assert not result.passed
        assert not result.required
        assert "missing" in result.message


class TestCheckBiffFile:
    @patch("biff.doctor.find_git_root", return_value=None)
    def test_no_git_repo(self, _mock: object) -> None:
        result = _check_biff_file()
        assert not result.passed
        assert not result.required

    def test_config_yaml_exists(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("relay:\n  url: tls://example\n")
        with patch("biff.doctor.find_git_root", return_value=tmp_path):
            result = _check_biff_file()
        assert result.passed

    def test_zero_config_passes(self, tmp_path: Path) -> None:
        """No config.yaml still passes -- zero-config is valid."""
        with patch("biff.doctor.find_git_root", return_value=tmp_path):
            result = _check_biff_file()
        assert result.passed
        assert "zero-config" in result.message


class TestCheckStatusline:
    def test_configured(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "/path/to/biff statusline",
                    }
                }
            )
        )
        with patch("biff.doctor.SETTINGS_PATH", settings):
            result = _check_statusline()
        assert result.passed
        assert not result.required

    def test_not_configured(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(json.dumps({}))
        with patch("biff.doctor.SETTINGS_PATH", settings):
            result = _check_statusline()
        assert not result.passed
        assert not result.required

    def test_corrupt_settings(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text("not valid json{{{")
        with patch("biff.doctor.SETTINGS_PATH", settings):
            result = _check_statusline()
        assert not result.passed
        assert not result.required
        assert "could not read" in result.message

    def test_configured_non_biff(self, tmp_path: Path) -> None:
        settings = tmp_path / "settings.json"
        settings.write_text(
            json.dumps(
                {
                    "statusLine": {
                        "type": "command",
                        "command": "some-other-tool status",
                    }
                }
            )
        )
        with patch("biff.doctor.SETTINGS_PATH", settings):
            result = _check_statusline()
        assert not result.passed
        assert not result.required


class TestResolveRelayConfig:
    @patch("biff.doctor.find_git_root", return_value=None)
    def test_defaults_to_demo_relay(self, _mock: object) -> None:
        url, auth = _resolve_relay_config()
        assert "ngs.global" in url
        assert auth is not None

    def test_uses_yaml_config(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: nats://custom:4222\n  auth:\n    token: s3cret\n"
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
    @patch("biff.doctor._check_user_commands")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_gh_cli")
    def test_all_pass_returns_zero(
        self,
        mock_gh: object,
        mock_plugin: object,
        mock_ucmds: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        for mock in [mock_plugin, mock_relay]:
            mock.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        for mock in [mock_gh, mock_ucmds, mock_biff, mock_sl]:
            mock.return_value = CheckResult("test", True, "ok", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 0

    @patch("biff.doctor._check_statusline")
    @patch("biff.doctor._check_biff_file")
    @patch("biff.doctor._check_relay")
    @patch("biff.doctor._check_user_commands")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_gh_cli")
    def test_required_failure_returns_one(
        self,
        mock_gh: object,
        mock_plugin: object,
        mock_ucmds: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        mock_plugin.return_value = CheckResult("plugin", False, "missing")  # type: ignore[attr-defined]
        mock_relay.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        for mock in [mock_gh, mock_ucmds, mock_biff, mock_sl]:
            mock.return_value = CheckResult("test", True, "ok", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 1

    @patch("biff.doctor._check_statusline")
    @patch("biff.doctor._check_biff_file")
    @patch("biff.doctor._check_relay")
    @patch("biff.doctor._check_user_commands")
    @patch("biff.doctor._check_plugin_installed")
    @patch("biff.doctor._check_gh_cli")
    def test_optional_failure_still_passes(
        self,
        mock_gh: object,
        mock_plugin: object,
        mock_ucmds: object,
        mock_relay: object,
        mock_biff: object,
        mock_sl: object,
    ) -> None:
        for mock in [mock_plugin, mock_relay]:
            mock.return_value = CheckResult("test", True, "ok")  # type: ignore[attr-defined]
        for mock in [mock_gh, mock_ucmds, mock_biff, mock_sl]:
            mock.return_value = CheckResult("test", False, "missing", required=False)  # type: ignore[attr-defined]

        assert check_environment() == 0
