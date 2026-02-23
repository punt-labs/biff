"""Tests for .biff.local config functions."""

from __future__ import annotations

from pathlib import Path

from biff.config import is_enabled, load_biff_local, write_biff_local


class TestLoadBiffLocal:
    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_biff_local(tmp_path) == {}

    def test_parses_enabled_true(self, tmp_path: Path) -> None:
        (tmp_path / ".biff.local").write_text("enabled = true\n")
        result = load_biff_local(tmp_path)
        assert result["enabled"] is True

    def test_parses_enabled_false(self, tmp_path: Path) -> None:
        (tmp_path / ".biff.local").write_text("enabled = false\n")
        result = load_biff_local(tmp_path)
        assert result["enabled"] is False

    def test_malformed_toml_returns_empty(self, tmp_path: Path) -> None:
        (tmp_path / ".biff.local").write_text("not valid toml [[[")
        assert load_biff_local(tmp_path) == {}


class TestIsEnabled:
    def test_none_repo_root(self) -> None:
        assert is_enabled(None) is False

    def test_no_biff_file(self, tmp_path: Path) -> None:
        assert is_enabled(tmp_path) is False

    def test_biff_but_no_local(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        assert is_enabled(tmp_path) is False

    def test_enabled_true(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        (tmp_path / ".biff.local").write_text("enabled = true\n")
        assert is_enabled(tmp_path) is True

    def test_enabled_false(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        (tmp_path / ".biff.local").write_text("enabled = false\n")
        assert is_enabled(tmp_path) is False

    def test_enabled_non_boolean(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        (tmp_path / ".biff.local").write_text('enabled = "yes"\n')
        assert is_enabled(tmp_path) is False

    def test_empty_local_file(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        (tmp_path / ".biff.local").write_text("")
        assert is_enabled(tmp_path) is False


class TestWriteBiffLocal:
    def test_writes_enabled_true(self, tmp_path: Path) -> None:
        write_biff_local(tmp_path, enabled=True)
        content = (tmp_path / ".biff.local").read_text()
        assert content == "enabled = true\n"

    def test_writes_enabled_false(self, tmp_path: Path) -> None:
        write_biff_local(tmp_path, enabled=False)
        content = (tmp_path / ".biff.local").read_text()
        assert content == "enabled = false\n"

    def test_overwrites_existing(self, tmp_path: Path) -> None:
        write_biff_local(tmp_path, enabled=True)
        write_biff_local(tmp_path, enabled=False)
        content = (tmp_path / ".biff.local").read_text()
        assert content == "enabled = false\n"

    def test_roundtrip(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        write_biff_local(tmp_path, enabled=True)
        assert is_enabled(tmp_path) is True
        write_biff_local(tmp_path, enabled=False)
        assert is_enabled(tmp_path) is False
