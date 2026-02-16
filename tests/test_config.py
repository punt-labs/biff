"""Tests for configuration discovery and loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from biff.config import (
    _DEFAULT_DATA_DIR_NAME,
    GitHubIdentity,
    _extract_biff_fields,
    compute_data_dir,
    find_git_root,
    get_github_identity,
    get_os_user,
    load_biff_file,
    load_config,
)
from biff.models import RelayAuth

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


# -- get_github_identity (GitHub CLI) --


class TestGetGithubIdentity:
    def test_returns_login_and_name(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\tJ Freeman\n"
            result = get_github_identity()
            assert result == GitHubIdentity(login="jmf-pobox", display_name="J Freeman")

    def test_returns_empty_display_name_when_null(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\t\n"
            result = get_github_identity()
            assert result is not None
            assert result.login == "jmf-pobox"
            assert result.display_name == ""

    def test_handles_login_only_output(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "jmf-pobox\n"
            result = get_github_identity()
            assert result is not None
            assert result.login == "jmf-pobox"
            assert result.display_name == ""

    def test_returns_none_on_failure(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_github_identity() is None

    def test_returns_none_when_empty(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "  \n"
            assert get_github_identity() is None

    def test_returns_none_when_gh_missing(self) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_github_identity() is None


# -- get_os_user --


class TestGetOsUser:
    def test_returns_username(self) -> None:
        with patch("biff.config.getpass.getuser", return_value="kai"):
            assert get_os_user() == "kai"

    def test_returns_none_on_error(self) -> None:
        with patch("biff.config.getpass.getuser", side_effect=OSError):
            assert get_os_user() is None


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

    def test_malformed_toml_exits(self, tmp_path: Path) -> None:
        (tmp_path / ".biff").write_text("this is not valid toml [[[")
        with pytest.raises(SystemExit, match="Failed to parse"):
            load_biff_file(tmp_path)


# -- _extract_biff_fields (auth) --


class TestExtractRelayAuth:
    def test_no_relay_section(self) -> None:
        _, _, auth = _extract_biff_fields({})
        assert auth is None

    def test_relay_url_only(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://localhost:4222"}}
        _, url, auth = _extract_biff_fields(raw)
        assert url == "nats://localhost:4222"
        assert auth is None

    def test_token_auth(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": "s3cret"}}
        _, _, auth = _extract_biff_fields(raw)
        assert auth == RelayAuth(token="s3cret")

    def test_nkeys_seed_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "nkeys_seed": "/path/to.nk"}
        }
        _, _, auth = _extract_biff_fields(raw)
        assert auth == RelayAuth(nkeys_seed="/path/to.nk")

    def test_user_credentials_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "user_credentials": "/path/to.creds"}
        }
        _, _, auth = _extract_biff_fields(raw)
        assert auth == RelayAuth(user_credentials="/path/to.creds")

    def test_mutual_exclusivity_exits(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "nats://host", "token": "x", "nkeys_seed": "/y"}
        }
        with pytest.raises(SystemExit, match="Conflicting auth"):
            _extract_biff_fields(raw)

    def test_all_three_exits(self) -> None:
        raw: dict[str, object] = {
            "relay": {
                "url": "nats://host",
                "token": "x",
                "nkeys_seed": "/y",
                "user_credentials": "/z",
            }
        }
        with pytest.raises(SystemExit, match="Conflicting auth"):
            _extract_biff_fields(raw)

    def test_empty_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": ""}}
        _, _, auth = _extract_biff_fields(raw)
        assert auth is None

    def test_non_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": 42}}
        _, _, auth = _extract_biff_fields(raw)
        assert auth is None


# -- load_config --


_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")
_KAI_NO_NAME = GitHubIdentity(login="kai", display_name="")
_FROM_GIT = GitHubIdentity(login="from-git", display_name="Git User")


class TestLoadConfig:
    def _setup_repo(self, tmp_path: Path) -> Path:
        """Create a minimal git repo with .biff config."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".biff").write_text(
            '[team]\nmembers = ["kai", "eric"]\n\n'
            '[relay]\nurl = "nats://localhost:4222"\n'
        )
        return tmp_path

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_full_discovery(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == "Kai Chen"
        assert resolved.config.team == ("kai", "eric")
        assert resolved.config.relay_url == "nats://localhost:4222"
        assert resolved.data_dir == Path("/tmp/biff") / repo.name
        assert resolved.repo_root == repo

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_custom_prefix(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo, prefix=Path("/var/spool"))
        assert resolved.data_dir == Path("/var/spool/biff") / repo.name

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_data_dir_override(self, _mock: object, tmp_path: Path) -> None:
        repo = self._setup_repo(tmp_path)
        custom = tmp_path / "custom"
        resolved = load_config(start=repo, data_dir_override=custom)
        assert resolved.data_dir == custom

    @patch("biff.config.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_takes_precedence(
        self, _mock: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path, user_override="from-cli")
        assert resolved.config.user == "from-cli"

    @patch("biff.config.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_clears_display_name(
        self, _mock: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path, user_override="from-cli")
        assert resolved.config.display_name == ""

    @patch("biff.config.get_os_user", return_value="jfreeman")
    @patch("biff.config.get_github_identity", return_value=None)
    def test_falls_back_to_os_user(
        self, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""

    @patch("biff.config.get_os_user", return_value=None)
    @patch("biff.config.get_github_identity", return_value=None)
    def test_exits_when_all_user_sources_fail(
        self, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        with pytest.raises(SystemExit, match="No user configured"):
            load_config(start=tmp_path)

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_repo_uses_default_data_dir(self, _mock: object, tmp_path: Path) -> None:
        # No .git directory — should fall back to _default
        resolved = load_config(start=tmp_path)
        assert resolved.data_dir == Path("/tmp/biff") / _DEFAULT_DATA_DIR_NAME
        assert resolved.repo_root is None

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_biff_file(self, _mock: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.team == ()
        assert resolved.config.relay_url is None

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_repo_with_data_dir_override(
        self, _mock: object, tmp_path: Path
    ) -> None:
        custom = tmp_path / "data"
        resolved = load_config(start=tmp_path, data_dir_override=custom)
        assert resolved.data_dir == custom
        assert resolved.repo_root is None

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_relay_auth_flows_through(self, _mock: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".biff").write_text(
            '[relay]\nurl = "tls://host"\nuser_credentials = "/creds"\n'
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_auth == RelayAuth(user_credentials="/creds")

    @patch("biff.config.get_github_identity", return_value=_KAI_NO_NAME)
    def test_empty_display_name_when_github_has_none(
        self, _mock: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == ""
