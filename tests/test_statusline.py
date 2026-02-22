"""Tests for biff status line integration."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from biff.__main__ import app
from biff.statusline import (
    InstallResult,
    SessionUnread,
    UninstallResult,
    _as_str_dict,
    _base_segments,
    _biff_segment,
    _context_segment,
    _cost_segment,
    _git_segment,
    _int_field,
    _parse_session_data,
    _read_session_unread,
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
        sl = settings["statusLine"]
        assert isinstance(sl, dict)
        assert sl["type"] == "command"
        assert "statusline" in sl["command"]

    def test_fresh_with_existing_object(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        obj: dict[str, object] = {
            "type": "command",
            "command": "/bin/mystatus",
        }
        write_settings(settings_path, {"statusLine": obj})

        result = install(settings_path, stash_path)

        assert result.installed
        assert read_stash(stash_path) == obj

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

    def test_restore_object(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"
        obj: dict[str, object] = {"type": "command", "command": "/bin/old"}
        biff_sl: dict[str, object] = {
            "type": "command",
            "command": "biff statusline",
        }
        write_settings(settings_path, {"statusLine": biff_sl})
        write_stash(stash_path, obj)

        result = uninstall(settings_path, stash_path)

        assert result.uninstalled
        assert read_settings(settings_path)["statusLine"] == obj

    def test_not_installed(self, tmp_path: Path):
        settings_path = tmp_path / "settings.json"
        stash_path = tmp_path / "stash.json"

        result = uninstall(settings_path, stash_path)

        assert result == UninstallResult(uninstalled=False, message="Not installed.")


# --- Session Data Parsing ---------------------------------------------------


class TestParseSessionData:
    def test_valid_json(self) -> None:
        result = _parse_session_data('{"model": "opus", "workspace": "/foo"}')
        assert result == {"model": "opus", "workspace": "/foo"}

    def test_empty_json(self) -> None:
        assert _parse_session_data("{}") == {}

    def test_invalid_json(self) -> None:
        assert _parse_session_data("not json") == {}

    def test_non_dict_json(self) -> None:
        assert _parse_session_data("[1, 2, 3]") == {}

    def test_empty_string(self) -> None:
        assert _parse_session_data("") == {}


class TestAsStrDict:
    def test_dict_passes_through(self) -> None:
        result = _as_str_dict({"a": 1, "b": "two"})
        assert result == {"a": 1, "b": "two"}

    def test_non_dict_returns_empty(self) -> None:
        assert _as_str_dict("string") == {}
        assert _as_str_dict(42) == {}
        assert _as_str_dict(None) == {}
        assert _as_str_dict([1, 2]) == {}


class TestIntField:
    def test_int_value(self) -> None:
        assert _int_field({"tokens": 5000}, "tokens") == 5000

    def test_float_value(self) -> None:
        assert _int_field({"tokens": 5000.5}, "tokens") == 5000

    def test_missing_key(self) -> None:
        assert _int_field({}, "tokens") == 0

    def test_non_numeric(self) -> None:
        assert _int_field({"tokens": "not a number"}, "tokens") == 0


# --- Base Segments (repo, context, cost) ------------------------------------


class TestGitSegment:
    def test_workspace_string(self, tmp_path: Path) -> None:
        workspace = tmp_path / "my-repo"
        workspace.mkdir()
        session: dict[str, object] = {"workspace": str(workspace)}
        with patch("biff.statusline._git_branch", return_value="main"):
            result = _git_segment(session)
        assert result == "my-repo:main"

    def test_workspace_object_with_project_dir(self, tmp_path: Path) -> None:
        workspace = tmp_path / "my-repo"
        workspace.mkdir()
        session: dict[str, object] = {
            "workspace": {
                "project_dir": str(workspace),
                "current_dir": str(workspace),
            }
        }
        with patch("biff.statusline._git_branch", return_value="feat/x"):
            result = _git_segment(session)
        assert result == "my-repo:feat/x"

    def test_workspace_object_falls_back_to_current_dir(self, tmp_path: Path) -> None:
        workspace = tmp_path / "another-repo"
        workspace.mkdir()
        session: dict[str, object] = {"workspace": {"current_dir": str(workspace)}}
        with patch("biff.statusline._git_branch", return_value=""):
            result = _git_segment(session)
        assert result == "another-repo"

    def test_no_git_shows_dir_only(self, tmp_path: Path) -> None:
        workspace = tmp_path / "plain-dir"
        workspace.mkdir()
        session: dict[str, object] = {"workspace": str(workspace)}
        with patch("biff.statusline._git_branch", return_value=""):
            result = _git_segment(session)
        assert result == "plain-dir"

    def test_missing_workspace(self) -> None:
        assert _git_segment({}) == ""

    def test_non_string_workspace(self) -> None:
        assert _git_segment({"workspace": 42}) == ""

    def test_empty_workspace(self) -> None:
        assert _git_segment({"workspace": ""}) == ""

    def test_empty_workspace_object(self) -> None:
        assert _git_segment({"workspace": {}}) == ""


class TestContextSegment:
    def test_low_usage(self) -> None:
        session: dict[str, object] = {
            "context_window": {
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 20000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        }
        result = _context_segment(session)
        assert result == "10%"
        assert "\033[" not in result  # no color for low usage

    def test_medium_usage_yellow(self) -> None:
        session: dict[str, object] = {
            "context_window": {
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 120000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        }
        result = _context_segment(session)
        assert "60%" in result
        assert "\033[33m" in result  # yellow

    def test_high_usage_red(self) -> None:
        session: dict[str, object] = {
            "context_window": {
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 170000,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            }
        }
        result = _context_segment(session)
        assert "85%" in result
        assert "\033[31m" in result  # red

    def test_includes_cache_tokens(self) -> None:
        session: dict[str, object] = {
            "context_window": {
                "context_window_size": 200000,
                "current_usage": {
                    "input_tokens": 40000,
                    "cache_creation_input_tokens": 5000,
                    "cache_read_input_tokens": 10000,
                },
            }
        }
        result = _context_segment(session)
        # (40000+5000+10000)/200000 = 27%
        assert result == "27%"

    def test_missing_context_window(self) -> None:
        assert _context_segment({}) == ""

    def test_zero_window_size(self) -> None:
        session: dict[str, object] = {
            "context_window": {
                "context_window_size": 0,
                "current_usage": {"input_tokens": 100},
            }
        }
        assert _context_segment(session) == ""

    def test_non_dict_context_window(self) -> None:
        assert _context_segment({"context_window": "bad"}) == ""

    def test_used_percentage_field(self) -> None:
        session: dict[str, object] = {"context_window": {"used_percentage": 42}}
        assert _context_segment(session) == "42%"

    def test_used_percentage_yellow(self) -> None:
        session: dict[str, object] = {"context_window": {"used_percentage": 65}}
        result = _context_segment(session)
        assert "65%" in result
        assert "\033[33m" in result

    def test_used_percentage_red(self) -> None:
        session: dict[str, object] = {"context_window": {"used_percentage": 90}}
        result = _context_segment(session)
        assert "90%" in result
        assert "\033[31m" in result


class TestCostSegment:
    def test_positive_cost(self) -> None:
        session: dict[str, object] = {"cost": {"total_cost_usd": 1.23}}
        assert _cost_segment(session) == "$1.23"

    def test_zero_cost(self) -> None:
        session: dict[str, object] = {"cost": {"total_cost_usd": 0}}
        assert _cost_segment(session) == ""

    def test_missing_cost(self) -> None:
        assert _cost_segment({}) == ""

    def test_non_dict_cost(self) -> None:
        assert _cost_segment({"cost": "bad"}) == ""

    def test_small_cost(self) -> None:
        session: dict[str, object] = {"cost": {"total_cost_usd": 0.05}}
        assert _cost_segment(session) == "$0.05"


class TestBaseSegments:
    def test_empty_session(self) -> None:
        assert _base_segments({}) == []

    def test_full_session(self) -> None:
        session: dict[str, object] = {
            "workspace": {
                "project_dir": "/tmp/test-repo",
                "current_dir": "/tmp/test-repo",
            },
            "context_window": {"used_percentage": 25},
            "cost": {"total_cost_usd": 0.50},
        }
        segments = _base_segments(session)
        assert any("test-repo" in s for s in segments)
        assert "$0.50" in segments
        assert "25%" in segments


# --- Biff Segment -----------------------------------------------------------


class TestBiffSegment:
    def test_none_shows_plain(self) -> None:
        assert _biff_segment(None) == "biff"

    def test_zero_count_shows_identity(self) -> None:
        assert _biff_segment(SessionUnread("kai", 0, "tty1")) == "kai:tty1(0)"

    def test_user_with_tty(self) -> None:
        result = _biff_segment(SessionUnread("kai", 3, "tty1"))
        assert "kai:tty1(3)" in result
        assert "\033[1;33m" in result
        assert "\033[0m" in result

    def test_user_without_tty(self) -> None:
        result = _biff_segment(SessionUnread("kai", 3, ""))
        assert "kai(3)" in result
        assert "\033[1;33m" in result

    def test_empty_user_uses_biff(self) -> None:
        result = _biff_segment(SessionUnread("", 1, "tty1"))
        assert "biff:tty1(1)" in result

    def test_mesg_off_shows_n(self) -> None:
        result = _biff_segment(SessionUnread("kai", 5, "tty1", biff_enabled=False))
        assert result == "kai:tty1(n)"

    def test_mesg_off_no_bold(self) -> None:
        result = _biff_segment(SessionUnread("kai", 5, "tty1", biff_enabled=False))
        assert "\033[" not in result

    def test_mesg_off_without_tty(self) -> None:
        result = _biff_segment(SessionUnread("kai", 3, "", biff_enabled=False))
        assert result == "kai(n)"


# --- Read Session Unread ----------------------------------------------------


class TestReadSessionUnread:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert _read_session_unread(tmp_path / "nope.json") is None

    def test_valid_file(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text(
            json.dumps(
                {"user": "kai", "count": 5, "tty_name": "tty1", "preview": "@eric"}
            )
        )
        result = _read_session_unread(path)
        assert result == SessionUnread("kai", 5, "tty1")

    def test_zero_count(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text(
            json.dumps({"user": "kai", "count": 0, "tty_name": "", "preview": ""})
        )
        result = _read_session_unread(path)
        assert result is not None
        assert result.count == 0

    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text("not json")
        assert _read_session_unread(path) is None

    def test_missing_tty_name_defaults(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text(json.dumps({"user": "kai", "count": 2, "preview": ""}))
        result = _read_session_unread(path)
        assert result is not None
        assert result.tty_name == ""

    def test_biff_enabled_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text(
            json.dumps(
                {
                    "user": "kai",
                    "count": 3,
                    "tty_name": "tty1",
                    "preview": "",
                    "biff_enabled": False,
                }
            )
        )
        result = _read_session_unread(path)
        assert result is not None
        assert result.biff_enabled is False

    def test_missing_biff_enabled_defaults_true(self, tmp_path: Path) -> None:
        path = tmp_path / "12345.json"
        path.write_text(
            json.dumps({"user": "kai", "count": 2, "tty_name": "tty1", "preview": ""})
        )
        result = _read_session_unread(path)
        assert result is not None
        assert result.biff_enabled is True


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
        assert _run_original("__nonexistent_cmd_xyz__", "") is None

    def test_failing_command(self):
        assert _run_original("bash -c 'echo partial; exit 1'", "") is None


# --- Run Statusline (integration) ------------------------------------------


def _write_ppid_unread(
    unread_dir: Path, user: str, count: int, tty_name: str = "", preview: str = ""
) -> None:
    """Write a PPID-keyed unread file for the current process."""
    unread_dir.mkdir(parents=True, exist_ok=True)
    path = unread_dir / f"{os.getppid()}.json"
    path.write_text(
        json.dumps(
            {"user": user, "count": count, "tty_name": tty_name, "preview": preview}
        )
    )


class TestRunStatusline:
    def test_no_original_no_unreads(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_dir)
        assert result == "biff"

    def test_no_original_with_unreads(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 3, "tty1")
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_dir)
        assert "kai:tty1(3)" in result
        assert "\033[1;33m" in result

    def test_original_with_unreads(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 2, "tty1")
        write_stash(stash_path, {"type": "command", "command": "echo 42%"})
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_dir)
        assert "42%" in result
        assert "kai:tty1(2)" in result
        assert " | " in result

    def test_original_no_unreads(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        write_stash(stash_path, {"type": "command", "command": "echo 42%"})
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_dir)
        assert "42%" in result
        assert "biff" in result
        assert "(0)" not in result
        assert " | " in result

    def test_without_tty_name(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 1)
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "{}"
            result = run_statusline(stash_path, unread_dir)
        assert "kai(1)" in result
        assert ":" not in result.replace("\033[1;33m", "").replace("\033[0m", "")

    def test_native_segments_with_session_data(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 0, "tty1")
        session_json = json.dumps(
            {
                "workspace": {
                    "project_dir": "/tmp/my-repo",
                    "current_dir": "/tmp/my-repo",
                },
                "context_window": {
                    "used_percentage": 25,
                    "context_window_size": 200000,
                },
                "cost": {"total_cost_usd": 1.50},
            }
        )
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = session_json
            result = run_statusline(stash_path, unread_dir)
        assert "my-repo" in result
        assert "25%" in result
        assert "$1.50" in result
        assert "kai:tty1(0)" in result
        assert " | " in result

    def test_original_overrides_base_segments(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 0, "tty1")
        write_stash(stash_path, {"type": "command", "command": "echo custom-base"})
        session_json = json.dumps(
            {
                "workspace": {
                    "project_dir": "/tmp/my-repo",
                    "current_dir": "/tmp/my-repo",
                },
                "cost": {"total_cost_usd": 1.50},
            }
        )
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = session_json
            result = run_statusline(stash_path, unread_dir)
        # Original output replaces base segments
        assert "custom-base" in result
        assert "kai:tty1(0)" in result
        # Native segments should NOT appear when original is used
        assert "$1.50" not in result

    def test_empty_original_falls_back_to_native(self, tmp_path: Path) -> None:
        stash_path = tmp_path / "stash.json"
        unread_dir = tmp_path / "unread"
        _write_ppid_unread(unread_dir, "kai", 0, "tty1")
        write_stash(stash_path, {"type": "command", "command": "printf ''"})
        session_json = json.dumps(
            {
                "workspace": {
                    "project_dir": "/tmp/my-repo",
                    "current_dir": "/tmp/my-repo",
                },
                "context_window": {"used_percentage": 25},
                "cost": {"total_cost_usd": 1.50},
            }
        )
        with patch("biff.statusline.sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = session_json
            result = run_statusline(stash_path, unread_dir)
        # Empty successful original → its empty output is used (not native segments)
        # but the empty segment is filtered out, leaving just biff
        assert "kai:tty1(0)" in result


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
        biff_sl: dict[str, object] = {
            "type": "command",
            "command": "biff statusline",
        }
        write_settings(settings_path, {"statusLine": biff_sl})
        write_stash(stash_path, {"type": "command", "command": "echo old"})
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
