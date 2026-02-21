"""Tests for biff marketplace installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from biff.installer import (
    BIFF_COMMANDS,
    MARKETPLACE_KEY,
    PLUGIN_ID,
    TOOL_PERMISSION,
    _register_marketplace,
    _remove_commands,
    _remove_permissions,
    _unregister_marketplace,
    install,
    uninstall,
)

# -- Marketplace registration ------------------------------------------------


class TestRegisterMarketplace:
    def test_creates_file(self, tmp_path: Path) -> None:
        path = tmp_path / "known_marketplaces.json"
        result = _register_marketplace(path)
        assert result.passed
        data = json.loads(path.read_text())
        assert MARKETPLACE_KEY in data
        assert data[MARKETPLACE_KEY]["autoUpdate"] is True

    def test_idempotent(self, tmp_path: Path) -> None:
        path = tmp_path / "known_marketplaces.json"
        _register_marketplace(path)
        result = _register_marketplace(path)
        assert result.passed
        assert "already" in result.message

    def test_preserves_other_marketplaces(self, tmp_path: Path) -> None:
        path = tmp_path / "known_marketplaces.json"
        path.write_text(json.dumps({"other": {"source": "test"}}))
        _register_marketplace(path)
        data = json.loads(path.read_text())
        assert "other" in data
        assert MARKETPLACE_KEY in data


class TestUnregisterMarketplace:
    def test_removes_entry(self, tmp_path: Path) -> None:
        path = tmp_path / "known_marketplaces.json"
        _register_marketplace(path)
        # Patch home so _unregister_marketplace doesn't find real installed plugins
        with patch("biff.installer.Path.home", return_value=tmp_path):
            result = _unregister_marketplace(path)
        assert result.passed
        data = json.loads(path.read_text())
        assert MARKETPLACE_KEY not in data

    def test_noop_when_missing(self, tmp_path: Path) -> None:
        result = _unregister_marketplace(tmp_path / "nope.json")
        assert result.passed

    def test_keeps_if_other_plugins_installed(self, tmp_path: Path) -> None:
        mp_path = tmp_path / "known_marketplaces.json"
        _register_marketplace(mp_path)

        # Simulate another punt-labs plugin in the installed registry
        plugins_dir = tmp_path / ".claude" / "plugins"
        plugins_dir.mkdir(parents=True)
        reg_path = plugins_dir / "installed_plugins.json"
        reg_path.write_text(
            json.dumps({"plugins": {"dungeon@punt-labs": [{"scope": "user"}]}})
        )
        with patch("biff.installer.Path.home", return_value=tmp_path):
            result = _unregister_marketplace(mp_path)
        assert result.passed
        assert "kept" in result.message
        # Marketplace entry still present
        data = json.loads(mp_path.read_text())
        assert MARKETPLACE_KEY in data


# -- Command removal --------------------------------------------------------


class TestRemoveCommands:
    def test_removes_biff_commands(self, tmp_path: Path) -> None:
        for name in BIFF_COMMANDS:
            (tmp_path / name).write_text("test")
        result = _remove_commands(tmp_path)
        assert result.passed
        assert f"removed {len(BIFF_COMMANDS)}" in result.message
        assert not any((tmp_path / name).exists() for name in BIFF_COMMANDS)

    def test_preserves_non_biff_files(self, tmp_path: Path) -> None:
        (tmp_path / "custom.md").write_text("user's own command")
        for name in BIFF_COMMANDS:
            (tmp_path / name).write_text("test")
        _remove_commands(tmp_path)
        assert (tmp_path / "custom.md").exists()

    def test_noop_when_empty(self, tmp_path: Path) -> None:
        result = _remove_commands(tmp_path)
        assert result.passed
        assert "removed 0" in result.message


# -- Permission removal -----------------------------------------------------


class TestRemovePermissions:
    def test_removes_permission(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(
            json.dumps({"permissions": {"allow": [TOOL_PERMISSION, "other"]}})
        )
        result = _remove_permissions(path)
        assert result.passed
        settings = json.loads(path.read_text())
        assert TOOL_PERMISSION not in settings["permissions"]["allow"]
        assert "other" in settings["permissions"]["allow"]

    def test_noop_when_not_present(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text(json.dumps({"permissions": {"allow": ["other"]}}))
        result = _remove_permissions(path)
        assert result.passed

    def test_noop_when_no_file(self, tmp_path: Path) -> None:
        result = _remove_permissions(tmp_path / "nope.json")
        assert result.passed


# -- Plugin install/uninstall (mocked) --------------------------------------


class TestPluginInstall:
    @patch("biff.installer.shutil.which", return_value=None)
    def test_fails_without_claude(self, _mock: object) -> None:
        from biff.installer import _install_plugin

        result = _install_plugin()
        assert not result.passed
        assert "claude CLI not found" in result.message

    @patch("biff.installer.subprocess.run")
    @patch("biff.installer.shutil.which", return_value="/usr/bin/claude")
    def test_succeeds(self, _which: object, mock_run: object) -> None:
        from biff.installer import _install_plugin

        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        result = _install_plugin()
        assert result.passed
        mock_run.assert_called_once()  # type: ignore[attr-defined]
        args = mock_run.call_args[0][0]  # type: ignore[attr-defined]
        assert PLUGIN_ID in args
        assert "--scope" in args
        assert "user" in args


class TestPluginUninstall:
    @patch("biff.installer.shutil.which", return_value=None)
    def test_noop_without_claude(self, _mock: object) -> None:
        from biff.installer import _uninstall_plugin

        result = _uninstall_plugin()
        assert result.passed

    @patch("biff.installer.subprocess.run")
    @patch("biff.installer.shutil.which", return_value="/usr/bin/claude")
    def test_succeeds(self, _which: object, mock_run: object) -> None:
        from biff.installer import _uninstall_plugin

        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        result = _uninstall_plugin()
        assert result.passed


# -- Full install/uninstall --------------------------------------------------


class TestInstallOrchestrator:
    @patch("biff.installer._install_plugin")
    def test_install_runs_all_steps(self, mock_plugin: object, tmp_path: Path) -> None:
        from biff.installer import StepResult

        mock_plugin.return_value = StepResult("Plugin", True, "installed")  # type: ignore[attr-defined]
        result = install(marketplace_path=tmp_path / "known_marketplaces.json")
        assert result.installed
        assert len(result.steps) == 2

    @patch("biff.installer._install_plugin")
    def test_install_reports_failure(self, mock_plugin: object, tmp_path: Path) -> None:
        from biff.installer import StepResult

        mock_plugin.return_value = StepResult("Plugin", False, "failed")  # type: ignore[attr-defined]
        result = install(marketplace_path=tmp_path / "known_marketplaces.json")
        assert not result.installed


class TestUninstallOrchestrator:
    @patch("biff.installer._uninstall_plugin")
    @patch("biff.installer._unregister_marketplace")
    @patch("biff.statusline.uninstall")
    def test_uninstall_runs_all_steps(
        self,
        mock_sl: object,
        mock_unreg: object,
        mock_plugin: object,
        tmp_path: Path,
    ) -> None:
        from biff.installer import StepResult
        from biff.statusline import UninstallResult as SLResult

        mock_plugin.return_value = StepResult("Plugin", True, "uninstalled")  # type: ignore[attr-defined]
        mock_unreg.return_value = StepResult("Marketplace", True, "unregistered")  # type: ignore[attr-defined]
        mock_sl.return_value = SLResult(uninstalled=True, message="removed")  # type: ignore[attr-defined]

        # Create commands to remove
        commands_dir = tmp_path / "commands"
        commands_dir.mkdir()
        for name in BIFF_COMMANDS:
            (commands_dir / name).write_text("test")

        # Create settings with permission
        settings_path = tmp_path / "settings.json"
        settings_path.write_text(
            json.dumps({"permissions": {"allow": [TOOL_PERMISSION]}})
        )

        result = uninstall(
            commands_dir=commands_dir,
            settings_path=settings_path,
        )
        assert result.uninstalled
        assert len(result.steps) == 5


# -- CLI commands ------------------------------------------------------------


class TestCLICommands:
    def test_install_command_exists(self) -> None:
        from typer.testing import CliRunner

        from biff.__main__ import app

        runner = CliRunner()
        result = runner.invoke(app, ["install", "--help"])
        assert result.exit_code == 0
        assert "Install" in result.output

    def test_doctor_command_exists(self) -> None:
        from typer.testing import CliRunner

        from biff.__main__ import app

        runner = CliRunner()
        result = runner.invoke(app, ["doctor", "--help"])
        assert result.exit_code == 0
        assert "Check" in result.output

    def test_uninstall_command_exists(self) -> None:
        from typer.testing import CliRunner

        from biff.__main__ import app

        runner = CliRunner()
        result = runner.invoke(app, ["uninstall", "--help"])
        assert result.exit_code == 0
        assert "Uninstall" in result.output
