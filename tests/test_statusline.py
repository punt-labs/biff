"""Tests for biff status line integration."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from biff.__main__ import app
from biff.statusline import (
    InstallResult,
    UninstallResult,
    _biff_segment,
    _read_unread_count,
    _resolve_original_command,
    _run_original,
    install,
    read_settings,
    read_stash,
    run_statusline,
    uninstall,
    write_settings,
    write_stash,
)

runner = CliRunner()


# --- Settings I/O ----------------------------------------------------------


class TestSettingsIO:
    def test_round_trip(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        data: dict[str, object] = {"statusLine": "echo hi", "other": 42}
        write_settings(path, data)
        assert read_settings(path) == data

    def test_missing_file_returns_empty(self, tmp_path: Path):
        assert read_settings(tmp_path / "nope.json") == {}

    def test_preserves_other_keys(self, tmp_path: Path):
        path = tmp_path / "settings.json"
        original: dict[str, object] = {"theme": "dark", "statusLine": "old"}
        write_settings(path, original)
        loaded = read_settings(path)
        assert loaded["theme"] == "dark"

    def test_creates_parent_dirs(self, tmp_path: Path):
        path = tmp_path / "deep" / "nested" / "settings.json"
        write_settings(path, {"key": "val"})
        assert path.exists()


# --- Stash I/O -------------------------------------------------------------


class TestStashIO:
    def test_round_trip_none(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, None)
        assert read_stash(path) is None

    def test_round_trip_string(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, "echo hello")
        assert read_stash(path) == "echo hello"

    def test_round_trip_object(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        obj: dict[str, object] = {"command": "/usr/local/bin/mystatus"}
        write_stash(path, obj)
        assert read_stash(path) == {"command": "/usr/local/bin/mystatus"}

    def test_missing_file_returns_none(self, tmp_path: Path):
        assert read_stash(tmp_path / "nope.json") is None


# --- Install ----------------------------------------------------------------


class TestInstall:
    def test_fresh_no_existing(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"

        result = install(settings_path, stash_path)

        assert result == InstallResult(installed=True, message="Installed.")
        assert stash_path.exists()
        assert read_stash(stash_path) is None
        settings = read_settings(settings_path)
        assert "statusline" in settings["statusLine"]  # type: ignore[operator]

    def test_fresh_with_existing_string(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_settings(settings_path, {"statusLine": "echo old"})

        result = install(settings_path, stash_path)

        assert result.installed
        assert read_stash(stash_path) == "echo old"

    def test_fresh_with_existing_object(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        obj: dict[str, object] = {"command": "/bin/mystatus"}
        write_settings(settings_path, {"statusLine": obj})

        result = install(settings_path, stash_path)

        assert result.installed
        assert read_stash(stash_path) == {"command": "/bin/mystatus"}

    def test_already_installed(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_stash(stash_path, None)  # sentinel exists

        result = install(settings_path, stash_path)

        assert result == InstallResult(installed=False, message="Already installed.")

    def test_creates_settings_if_absent(self, tmp_path: Path):
        settings_path = tmp_path / "new" / "settings.json"
        stash_path = tmp_path / "stash.json"

        result = install(settings_path, stash_path)

        assert result.installed
        assert settings_path.exists()


# --- Uninstall --------------------------------------------------------------


class TestUninstall:
    def test_restore_none(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_settings(
            settings_path, {"statusLine": "biff statusline", "theme": "dark"}
        )
        write_stash(stash_path, None)

        result = uninstall(settings_path, stash_path)

        assert result == UninstallResult(uninstalled=True, message="Uninstalled.")
        settings = read_settings(settings_path)
        assert "statusLine" not in settings
        assert settings["theme"] == "dark"
        assert not stash_path.exists()

    def test_restore_string(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_settings(settings_path, {"statusLine": "biff statusline"})
        write_stash(stash_path, "echo original")

        result = uninstall(settings_path, stash_path)

        assert result.uninstalled
        assert read_settings(settings_path)["statusLine"] == "echo original"

    def test_restore_object(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        obj: dict[str, object] = {"command": "/bin/old"}
        write_settings(settings_path, {"statusLine": "biff statusline"})
        write_stash(stash_path, obj)

        result = uninstall(settings_path, stash_path)

        assert result.uninstalled
        assert read_settings(settings_path)["statusLine"] == {"command": "/bin/old"}

    def test_not_installed(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"

        result = uninstall(settings_path, stash_path)

        assert result == UninstallResult(uninstalled=False, message="Not installed.")


# --- Biff Segment -----------------------------------------------------------


class TestBiffSegment:
    def test_zero_count(self):
        assert _biff_segment(0) == "biff(0)"

    def test_positive_count(self):
        result = _biff_segment(3)
        assert "biff(3)" in result
        assert "\033[1;33m" in result  # bold yellow
        assert "\033[0m" in result  # reset


# --- Read Unread Count ------------------------------------------------------


class TestReadUnreadCount:
    def test_missing_file(self, tmp_path: Path):
        assert _read_unread_count(tmp_path / "nope.json") == 0

    def test_valid_count(self, tmp_path: Path):
        path = tmp_path / "unread.json"
        path.write_text(json.dumps({"count": 5}))
        assert _read_unread_count(path) == 5

    def test_zero_count(self, tmp_path: Path):
        path = tmp_path / "unread.json"
        path.write_text(json.dumps({"count": 0}))
        assert _read_unread_count(path) == 0

    def test_invalid_json(self, tmp_path: Path):
        path = tmp_path / "unread.json"
        path.write_text("not json")
        assert _read_unread_count(path) == 0

    def test_missing_count_key(self, tmp_path: Path):
        path = tmp_path / "unread.json"
        path.write_text(json.dumps({"other": "data"}))
        assert _read_unread_count(path) == 0


# --- Resolve Original Command -----------------------------------------------


class TestResolveOriginalCommand:
    def test_no_stash(self, tmp_path: Path):
        assert _resolve_original_command(tmp_path / "nope.json") is None

    def test_null_original(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, None)
        assert _resolve_original_command(path) is None

    def test_string_original(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, "echo hello")
        assert _resolve_original_command(path) == "echo hello"

    def test_object_with_command(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, {"command": "/bin/mystatus"})
        assert _resolve_original_command(path) == "/bin/mystatus"

    def test_object_without_command(self, tmp_path: Path):
        path = tmp_path / "stash.json"
        write_stash(path, {"other": "value"})
        assert _resolve_original_command(path) is None


# --- Run Original -----------------------------------------------------------


class TestRunOriginal:
    def test_simple_command(self):
        assert _run_original("echo hello", "") == "hello"

    def test_stdin_passthrough(self):
        result = _run_original("cat", "input data")
        assert result == "input data"

    def test_bad_command(self):
        assert _run_original("__nonexistent_cmd_xyz__", "") == ""


# --- Run Statusline (integration) ------------------------------------------


class TestRunStatusline:
    def test_no_original_no_unreads(self, tmp_path: Path):
        stash_path = tmp_path / "stash.json"
        unread_path = tmp_path / "unread.json"
        # No stash file, no unread file
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_path)
        assert result == "biff(0)"

    def test_no_original_with_unreads(self, tmp_path: Path):
        stash_path = tmp_path / "stash.json"
        unread_path = tmp_path / "unread.json"
        unread_path.write_text(json.dumps({"count": 3}))
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_path)
        assert "biff(3)" in result
        assert "\033[1;33m" in result

    def test_original_with_unreads(self, tmp_path: Path):
        stash_path = tmp_path / "stash.json"
        unread_path = tmp_path / "unread.json"
        write_stash(stash_path, "echo 42%")
        unread_path.write_text(json.dumps({"count": 2}))
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_path)
        assert "42%" in result
        assert "biff(2)" in result
        assert " | " in result

    def test_original_no_unreads(self, tmp_path: Path):
        stash_path = tmp_path / "stash.json"
        unread_path = tmp_path / "unread.json"
        write_stash(stash_path, "echo 42%")
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_path)
        assert "42%" in result
        assert "biff(0)" in result
        assert " | " in result


# --- CLI integration -------------------------------------------------------


class TestCLI:
    def test_install_fresh(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        with (
            patch("biff.statusline.SETTINGS_PATH", settings_path),
            patch("biff.statusline.STASH_PATH", stash_path),
        ):
            result = runner.invoke(app, ["install-statusline"])
        assert result.exit_code == 0
        assert "Installed" in result.output

    def test_install_already_installed(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_stash(stash_path, None)
        with (
            patch("biff.statusline.SETTINGS_PATH", settings_path),
            patch("biff.statusline.STASH_PATH", stash_path),
        ):
            result = runner.invoke(app, ["install-statusline"])
        assert result.exit_code == 1
        assert "Already installed" in result.output

    def test_uninstall(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        write_settings(settings_path, {"statusLine": "biff statusline"})
        write_stash(stash_path, "echo old")
        with (
            patch("biff.statusline.SETTINGS_PATH", settings_path),
            patch("biff.statusline.STASH_PATH", stash_path),
        ):
            result = runner.invoke(app, ["uninstall-statusline"])
        assert result.exit_code == 0
        assert "Uninstalled" in result.output

    def test_uninstall_not_installed(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        with (
            patch("biff.statusline.SETTINGS_PATH", settings_path),
            patch("biff.statusline.STASH_PATH", stash_path),
        ):
            result = runner.invoke(app, ["uninstall-statusline"])
        assert result.exit_code == 1
        assert "Not installed" in result.output
