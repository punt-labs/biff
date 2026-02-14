"""Tests for configuration discovery and loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from biff.config import (
    compute_data_dir,
    find_git_root,
    get_git_user,
    load_biff_file,
    load_config,
)

# -- find_git_root --


class TestFindGitRoot:
    def test_finds_root(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert find_git_root(tmp_path) == tmp_path

    def test_finds_root_from_nested_dir(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        nested = tmp_path / "a" / "b" / "c"
        nested.mkdir(parents=True)
        assert find_git_root(nested) == tmp_path

    def test_returns_none_when_no_git(self, tmp_path: Path) -> None:
        assert find_git_root(tmp_path) is None


# -- get_git_user --


class TestGetGitUser:
    def test_returns_value_when_set(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\n"
            assert get_git_user() == "jmf-pobox"

    def test_returns_none_when_unset(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_git_user() is None

    def test_returns_none_when_empty(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  \n"
            assert get_git_user() is None

    def test_returns_none_when_git_missing(self) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_git_user() is None


# -- compute_data_dir --


class TestComputeDataDir:
    def test_basic(self) -> None:
        root = Path("/home/kai/projects/myapp")
        result = compute_data_dir(root, Path("/tmp"))
        assert result == Path("/tmp/biff/myapp")

    def test_var_spool_prefix(self) -> None:
        root = Path("/home/kai/projects/myapp")
        result = compute_data_dir(root, Path("/var/spool"))
        assert result == Path("/var/spool/biff/myapp")


# -- load_biff_file --


class TestLoadBiffFile:
    def test_parses_full_config(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text(
            '[team]\nmembers = ["kai", "eric"]\n\n[relay]\nurl = "nats://localhost"\n'
        )
        result = load_biff_file(tmp_path)
        assert result["team"] == {"members": ["kai", "eric"]}
        assert result["relay"] == {"url": "nats://localhost"}

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_biff_file(tmp_path) == {}

    def test_team_only(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
        result = load_biff_file(tmp_path)
        assert "team" in result
        assert "relay" not in result

    def test_empty_file(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("")
        assert load_biff_file(tmp_path) == {}


# -- load_config --


class TestLoadConfig:
    def _setup_repo(self, tmp_path: Path) -> Path:
        """Create a minimal git repo with .biff config."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".biff").write_text(
            '[team]\nmembers = ["kai", "eric"]\n\n'
            '[relay]\nurl = "nats://localhost:4222"\n'
        )
        return tmp_path

    @patch("biff.config.get_git_user", return_value="kai")
    def test_full_discovery(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.user == "kai"
        assert resolved.config.team == ("kai", "eric")
        assert resolved.config.relay_url == "nats://localhost:4222"
        assert resolved.data_dir == Path("/tmp/biff") / repo.name
        assert resolved.repo_root == repo

    @patch("biff.config.get_git_user", return_value="kai")
    def test_custom_prefix(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo, prefix=Path("/var/spool"))
        assert resolved.data_dir == Path("/var/spool/biff") / repo.name

    @patch("biff.config.get_git_user", return_value="kai")
    def test_data_dir_override(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        custom = tmp_path / "custom"
        resolved = load_config(start=repo, data_dir_override=custom)
        assert resolved.data_dir == custom

    @patch("biff.config.get_git_user", return_value="from-git")
    def test_user_override_takes_precedence(
        self, _mock: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path, user_override="from-cli")
        assert resolved.config.user == "from-cli"

    @patch("biff.config.get_git_user", return_value=None)
    def test_exits_when_no_user(self, _mock: object, tmp_path: Path) -> None:
        self._setup_repo(tmp_path)
        with pytest.raises(SystemExit, match="No user configured"):
            load_config(start=tmp_path)

    @patch("biff.config.get_git_user", return_value="kai")
    def test_exits_when_no_repo_and_no_data_dir(
        self, _mock: object, tmp_path: Path
    ) -> None:
        # No .git directory
        with pytest.raises(SystemExit, match="Cannot determine data directory"):
            load_config(start=tmp_path)

    @patch("biff.config.get_git_user", return_value="kai")
    def test_no_biff_file(self, _mock: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.team == ()
        assert resolved.config.relay_url is None

    @patch("biff.config.get_git_user", return_value="kai")
    def test_no_repo_with_data_dir_override(
        self, _mock: object, tmp_path: Path
    ) -> None:
        custom = tmp_path / "data"
        resolved = load_config(start=tmp_path, data_dir_override=custom)
        assert resolved.data_dir == custom
        assert resolved.repo_root is None
