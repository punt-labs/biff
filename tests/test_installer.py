"""Tests for biff installer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from biff.installer import (
    PLUGIN_KEY,
    _disable_plugin,
    _enable_plugin,
    _install_plugin_files,
    _install_user_commands,
    _read_registry,
    _register_plugin,
    _uninstall_plugin_files,
    _uninstall_user_commands,
    _unregister_plugin,
    install,
    uninstall,
)
from biff.statusline import read_settings, write_settings

# -- Plugin files ------------------------------------------------------------


class TestInstallPluginFiles:
    def test_copies_plugin_tree(self, tmp_path: Path) -> None:
        target = tmp_path / "plugins" / "biff"
        result = _install_plugin_files(target)
        assert result.passed
        assert (target / "commands").is_dir()
        commands = list((target / "commands").glob("*.md"))
        assert len(commands) >= 1

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "plugins" / "biff"
        target.mkdir(parents=True)
        (target / "stale.txt").write_text("old")
        result = _install_plugin_files(target)
        assert result.passed
        assert not (target / "stale.txt").exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "nested" / "biff"
        result = _install_plugin_files(target)
        assert result.passed
        assert target.exists()


class TestUninstallPluginFiles:
    def test_removes_directory(self, tmp_path: Path) -> None:
        target = tmp_path / "plugins" / "biff"
        _install_plugin_files(target)
        result = _uninstall_plugin_files(target)
        assert result.passed
        assert not target.exists()

    def test_noop_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent"
        result = _uninstall_plugin_files(target)
        assert result.passed


# -- User commands -----------------------------------------------------------


class TestInstallUserCommands:
    def test_deploys_all_md_files(self, tmp_path: Path) -> None:
        target = tmp_path / "commands"
        result = _install_user_commands(target)
        assert result.passed
        deployed = sorted(f.name for f in target.glob("*.md"))
        assert len(deployed) >= 1
        assert "who.md" in deployed

    def test_creates_parent_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "deep" / "commands"
        result = _install_user_commands(target)
        assert result.passed
        assert target.exists()

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        target = tmp_path / "commands"
        target.mkdir()
        (target / "who.md").write_text("stale")
        result = _install_user_commands(target)
        assert result.passed
        assert (target / "who.md").read_text() != "stale"


class TestUninstallUserCommands:
    def test_removes_biff_commands(self, tmp_path: Path) -> None:
        target = tmp_path / "commands"
        _install_user_commands(target)
        result = _uninstall_user_commands(target)
        assert result.passed
        assert not list(target.glob("*.md"))

    def test_noop_when_missing(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent"
        result = _uninstall_user_commands(target)
        assert result.passed
        assert "removed 0" in result.message

    def test_preserves_non_biff_files(self, tmp_path: Path) -> None:
        target = tmp_path / "commands"
        _install_user_commands(target)
        (target / "custom.md").write_text("user's own command")
        _uninstall_user_commands(target)
        assert (target / "custom.md").exists()
        assert (target / "custom.md").read_text() == "user's own command"


# -- Plugin registry ---------------------------------------------------------


class TestRegistry:
    def test_read_missing_returns_empty(self, tmp_path: Path) -> None:
        assert _read_registry(tmp_path / "nope.json") == {}

    def test_read_invalid_json_returns_empty(self, tmp_path: Path) -> None:
        path = tmp_path / "bad.json"
        path.write_text("not json!")
        assert _read_registry(path) == {}

    def test_register_creates_entry(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "installed_plugins.json"
        plugins_dir = tmp_path / "plugins" / "biff"
        result = _register_plugin(reg_path, plugins_dir)
        assert result.passed

        data = json.loads(reg_path.read_text())
        assert PLUGIN_KEY in data["plugins"]
        entry = data["plugins"][PLUGIN_KEY][0]
        assert entry["installPath"] == str(plugins_dir)
        assert entry["scope"] == "user"

    def test_register_preserves_other_plugins(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "installed_plugins.json"
        reg_path.write_text(json.dumps({"plugins": {"other@1.0": [{"scope": "user"}]}}))
        _register_plugin(reg_path, tmp_path / "biff")
        data = json.loads(reg_path.read_text())
        assert "other@1.0" in data["plugins"]
        assert PLUGIN_KEY in data["plugins"]

    def test_unregister_removes_entry(self, tmp_path: Path) -> None:
        reg_path = tmp_path / "installed_plugins.json"
        _register_plugin(reg_path, tmp_path / "biff")
        result = _unregister_plugin(reg_path)
        assert result.passed
        data = json.loads(reg_path.read_text())
        assert PLUGIN_KEY not in data.get("plugins", {})

    def test_unregister_noop_when_missing(self, tmp_path: Path) -> None:
        result = _unregister_plugin(tmp_path / "nope.json")
        assert result.passed


# -- Plugin enable/disable ---------------------------------------------------


class TestPluginSettings:
    def test_enable_sets_flag(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        result = _enable_plugin(settings_path)
        assert result.passed
        settings = read_settings(settings_path)
        enabled = settings["enabledPlugins"]
        assert isinstance(enabled, dict)
        assert enabled[PLUGIN_KEY] is True

    def test_enable_preserves_existing(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        write_settings(settings_path, {"theme": "dark"})
        _enable_plugin(settings_path)
        settings = read_settings(settings_path)
        assert settings["theme"] == "dark"
        enabled = settings["enabledPlugins"]
        assert isinstance(enabled, dict)
        assert enabled[PLUGIN_KEY] is True

    def test_disable_removes_flag(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        _enable_plugin(settings_path)
        result = _disable_plugin(settings_path)
        assert result.passed
        settings = read_settings(settings_path)
        enabled = settings.get("enabledPlugins")
        assert not isinstance(enabled, dict) or PLUGIN_KEY not in enabled

    def test_disable_noop_when_not_enabled(self, tmp_path: Path) -> None:
        settings_path = tmp_path / "settings.json"
        write_settings(settings_path, {"other": True})
        result = _disable_plugin(settings_path)
        assert result.passed

    def test_disable_noop_when_no_file(self, tmp_path: Path) -> None:
        result = _disable_plugin(tmp_path / "nope.json")
        assert result.passed


# -- MCP server (mocked) ----------------------------------------------------


class TestMcpServer:
    @patch("biff.installer.shutil.which", return_value=None)
    def test_install_fails_without_claude(self, _mock: object) -> None:
        from biff.installer import _install_mcp_server

        result = _install_mcp_server()
        assert not result.passed
        assert "claude CLI not found" in result.message

    @patch("biff.installer.subprocess.run")
    @patch("biff.installer.shutil.which", return_value="/usr/bin/claude")
    def test_install_succeeds(self, _which: object, mock_run: object) -> None:
        from biff.installer import _install_mcp_server

        mock_run.return_value.returncode = 0  # type: ignore[attr-defined]
        result = _install_mcp_server()
        assert result.passed

    @patch("biff.installer.shutil.which", return_value=None)
    def test_uninstall_noop_without_claude(self, _mock: object) -> None:
        from biff.installer import _uninstall_mcp_server

        result = _uninstall_mcp_server()
        assert result.passed  # Not an error — nothing to remove


# -- Full install/uninstall --------------------------------------------------


class TestInstallOrchestrator:
    @patch("biff.installer._install_mcp_server")
    def test_install_runs_all_steps(self, mock_mcp: object, tmp_path: Path) -> None:
        from biff.installer import StepResult

        mock_mcp.return_value = StepResult("MCP server", True, "registered")  # type: ignore[attr-defined]
        result = install(
            plugins_dir=tmp_path / "plugins" / "biff",
            settings_path=tmp_path / "settings.json",
            registry_path=tmp_path / "installed_plugins.json",
            commands_dir=tmp_path / "commands",
        )
        assert result.installed
        assert len(result.steps) == 5
        assert all(s.passed for s in result.steps)

    @patch("biff.installer._install_mcp_server")
    def test_install_reports_failure(self, mock_mcp: object, tmp_path: Path) -> None:
        from biff.installer import StepResult

        mock_mcp.return_value = StepResult("MCP server", False, "failed")  # type: ignore[attr-defined]
        result = install(
            plugins_dir=tmp_path / "plugins" / "biff",
            settings_path=tmp_path / "settings.json",
            registry_path=tmp_path / "installed_plugins.json",
            commands_dir=tmp_path / "commands",
        )
        assert not result.installed
        assert any(not s.passed for s in result.steps)


class TestUninstallOrchestrator:
    @patch("biff.installer._uninstall_mcp_server")
    @patch("biff.statusline.uninstall")
    def test_uninstall_runs_all_steps(
        self, mock_sl: object, mock_mcp: object, tmp_path: Path
    ) -> None:
        from biff.installer import StepResult
        from biff.statusline import UninstallResult as SLResult

        mock_mcp.return_value = StepResult("MCP server", True, "removed")  # type: ignore[attr-defined]
        mock_sl.return_value = SLResult(uninstalled=True, message="removed")  # type: ignore[attr-defined]

        # Set up state to uninstall
        _install_plugin_files(tmp_path / "plugins" / "biff")
        _register_plugin(
            tmp_path / "installed_plugins.json",
            tmp_path / "plugins" / "biff",
        )
        _enable_plugin(tmp_path / "settings.json")

        # Install user commands so uninstall has something to remove
        _install_user_commands(tmp_path / "commands")

        result = uninstall(
            plugins_dir=tmp_path / "plugins" / "biff",
            settings_path=tmp_path / "settings.json",
            registry_path=tmp_path / "installed_plugins.json",
            commands_dir=tmp_path / "commands",
        )
        assert result.uninstalled
        assert len(result.steps) == 6  # 5 core + statusline


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
