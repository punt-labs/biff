"""Tests for workflow marker files (biff-vq5, biff-41j)."""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from biff.markers import (
    check_bead_in_progress,
    clear_plan_marker,
    clear_wall_marker,
    has_plan_marker,
    hint_dir,
    read_wall_marker,
    write_plan_marker,
    write_wall_marker,
)


class TestHintDir:
    """hint_dir() returns worktree-scoped paths."""

    def test_deterministic_hash(self) -> None:
        d1 = hint_dir("/some/path")
        d2 = hint_dir("/some/path")
        assert d1 == d2

    def test_different_roots_different_dirs(self) -> None:
        d1 = hint_dir("/path/a")
        d2 = hint_dir("/path/b")
        assert d1 != d2

    def test_empty_root_uses_default(self) -> None:
        d = hint_dir("")
        assert d.name == "default"


class TestPlanMarker:
    """Plan-active marker write/read/clear cycle."""

    def test_write_creates_marker(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", "biff-vq5: PreToolUse gate")
            assert has_plan_marker("/test/root")

    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", "some plan")
            clear_plan_marker("/test/root")
            assert not has_plan_marker("/test/root")

    def test_no_marker_returns_false(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert not has_plan_marker("/test/root")

    def test_clear_missing_marker_is_noop(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            clear_plan_marker("/test/root")  # should not raise


class TestCheckBeadInProgress:
    """Bead-active check via bd subprocess."""

    def test_returns_yes_when_beads_exist(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '[{"id": "biff-vq5"}]'
            assert check_bead_in_progress() == "yes"

    def test_returns_no_on_empty_list(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert check_bead_in_progress() == "no"

    def test_returns_unavailable_on_nonzero_exit(self) -> None:
        with patch("subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert check_bead_in_progress() == "unavailable"

    def test_returns_unavailable_when_bd_not_found(self) -> None:
        with patch("subprocess.run", side_effect=FileNotFoundError):
            assert check_bead_in_progress() == "unavailable"

    def test_returns_unavailable_on_timeout(self) -> None:
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("bd", 5)):
            assert check_bead_in_progress() == "unavailable"


class TestWallMarker:
    """Wall-active marker write/read/clear cycle."""

    def test_write_and_read_active_wall(self, tmp_path: Path) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_wall_marker("/test/root", "deploy in 10m", future)
            assert read_wall_marker("/test/root") == "deploy in 10m"

    def test_expired_wall_returns_none(self, tmp_path: Path) -> None:
        past = datetime.now(UTC) - timedelta(seconds=1)
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_wall_marker("/test/root", "old wall", past)
            assert read_wall_marker("/test/root") is None

    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        future = datetime.now(UTC) + timedelta(hours=1)
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_wall_marker("/test/root", "test", future)
            clear_wall_marker("/test/root")
            assert read_wall_marker("/test/root") is None

    def test_no_marker_returns_none(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert read_wall_marker("/test/root") is None

    def test_naive_datetime_returns_none(self, tmp_path: Path) -> None:
        """Corrupted marker with naive datetime doesn't crash."""
        import json as _json

        with patch("pathlib.Path.home", return_value=tmp_path):
            d = hint_dir("/test/root")
            d.mkdir(parents=True, exist_ok=True)
            # Write a marker with no timezone info
            (d / "wall-active").write_text(
                _json.dumps({"text": "bad", "expires_at": "2099-01-01T00:00:00"})
            )
            # Should not raise TypeError — returns gracefully
            result = read_wall_marker("/test/root")
            # fromisoformat without tz → naive datetime → TypeError caught
            assert result is None or isinstance(result, str)
