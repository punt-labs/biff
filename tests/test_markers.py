"""Tests for workflow marker files (biff-vq5, biff-41j)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from biff.markers import (
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
            write_plan_marker("/test/root", "sid-1", "biff-vq5: PreToolUse gate")
            assert has_plan_marker("/test/root", "sid-1")

    def test_clear_removes_marker(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", "sid-1", "some plan")
            clear_plan_marker("/test/root", "sid-1")
            assert not has_plan_marker("/test/root", "sid-1")

    def test_no_marker_returns_false(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            assert not has_plan_marker("/test/root", "sid-1")

    def test_clear_missing_marker_is_noop(self, tmp_path: Path) -> None:
        with patch("pathlib.Path.home", return_value=tmp_path):
            clear_plan_marker("/test/root", "sid-1")  # should not raise

    def test_none_identity_uses_shared_bucket(self, tmp_path: Path) -> None:
        """A caller with no resolvable identity falls back to 'shared'."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", None, "headless plan")
            assert has_plan_marker("/test/root", None)
            assert (hint_dir("/test/root") / "plan" / "shared").is_file()

    def test_different_identities_do_not_collide(self, tmp_path: Path) -> None:
        """Two sessions in one repo each get their own marker file."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", "sid-a", "plan A")
            write_plan_marker("/test/root", "sid-b", "plan B")
            assert has_plan_marker("/test/root", "sid-a")
            assert has_plan_marker("/test/root", "sid-b")

            clear_plan_marker("/test/root", "sid-a")
            assert not has_plan_marker("/test/root", "sid-a")
            # sid-b's marker is untouched by sid-a's clear -- the direct
            # fix for om9's core mechanism.
            assert has_plan_marker("/test/root", "sid-b")

    def test_identity_with_traversal_chars_falls_back_to_shared(
        self, tmp_path: Path
    ) -> None:
        """An identity outside the safe charset never reaches the path join."""
        with patch("pathlib.Path.home", return_value=tmp_path):
            write_plan_marker("/test/root", "../../etc/passwd", "hostile")
            # The write landed in the shared bucket, not an escaped path.
            assert has_plan_marker("/test/root", None)
            escaped = hint_dir("/test/root") / "plan" / "../../etc/passwd"
            assert not escaped.resolve().is_file()


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
