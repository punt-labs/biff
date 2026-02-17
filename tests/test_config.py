"""Tests for configuration discovery and loading."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from biff.config import (
    GitHubIdentity,
    _parse_repo_slug,
    compute_data_dir,
    extract_biff_fields,
    find_git_root,
    get_github_identity,
    get_os_user,
    get_repo_slug,
    load_biff_file,
    load_config,
    sanitize_repo_name,
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


# -- _parse_repo_slug --


class TestParseRepoSlug:
    def test_ssh_url(self) -> None:
        assert _parse_repo_slug("git@github.com:punt-labs/biff.git") == "punt-labs/biff"

    def test_ssh_url_no_dotgit(self) -> None:
        assert _parse_repo_slug("git@github.com:punt-labs/biff") == "punt-labs/biff"

    def test_https_url(self) -> None:
        url = "https://github.com/punt-labs/biff.git"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    def test_https_url_no_dotgit(self) -> None:
        url = "https://github.com/punt-labs/biff"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    def test_ssh_scheme_url(self) -> None:
        url = "ssh://git@github.com/punt-labs/biff.git"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    def test_ssh_scheme_url_no_dotgit(self) -> None:
        url = "ssh://git@github.com/punt-labs/biff"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    def test_ssh_scheme_url_with_port(self) -> None:
        url = "ssh://git@github.com:2222/punt-labs/biff.git"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    def test_nested_path_rejected(self) -> None:
        url = "https://gitlab.com/group/sub/repo.git"
        assert _parse_repo_slug(url) is None

    def test_non_url_returns_none(self) -> None:
        assert _parse_repo_slug("/local/path/to/repo") is None

    def test_bare_name_returns_none(self) -> None:
        assert _parse_repo_slug("biff") is None


# -- get_repo_slug --


class TestGetRepoSlug:
    def test_success(self, tmp_path: Path) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "git@github.com:punt-labs/biff.git\n"
            assert get_repo_slug(tmp_path) == "punt-labs/biff"

    def test_no_remote_returns_none(self, tmp_path: Path) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            assert get_repo_slug(tmp_path) is None

    def test_git_not_found_returns_none(self, tmp_path: Path) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_repo_slug(tmp_path) is None


# -- sanitize_repo_name --


class TestSanitizeRepoName:
    def test_simple_name(self) -> None:
        assert sanitize_repo_name("myapp") == "myapp"

    def test_dots_become_dashes(self) -> None:
        assert sanitize_repo_name("my.app.v2") == "my-app-v2"

    def test_spaces_become_dashes(self) -> None:
        assert sanitize_repo_name("my app") == "my-app"

    def test_strips_special_chars(self) -> None:
        assert sanitize_repo_name("my@app!v2") == "myappv2"

    def test_preserves_underscores(self) -> None:
        assert sanitize_repo_name("my_app") == "my_app"

    def test_preserves_dashes(self) -> None:
        assert sanitize_repo_name("my-app") == "my-app"

    def test_empty_exits(self) -> None:
        with pytest.raises(SystemExit, match="no usable characters"):
            sanitize_repo_name("")

    def test_all_special_exits(self) -> None:
        with pytest.raises(SystemExit, match="no usable characters"):
            sanitize_repo_name("@#$%")

    def test_slash_becomes_double_underscore(self) -> None:
        assert sanitize_repo_name("owner/repo") == "owner__repo"

    def test_slug_with_dots(self) -> None:
        assert sanitize_repo_name("owner/socket.io") == "owner__socket-io"

    def test_no_collision_with_underscored_names(self) -> None:
        assert sanitize_repo_name("a_b/c") != sanitize_repo_name("a/b_c")

    def test_nats_wildcards_stripped(self) -> None:
        assert sanitize_repo_name("app*>test") == "apptest"

    def test_unicode_stripped(self) -> None:
        assert sanitize_repo_name("café") == "caf"


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


# -- extract_biff_fields (auth) --


class TestExtractRelayAuth:
    def test_no_relay_section(self) -> None:
        _, _, auth = extract_biff_fields({})
        assert auth is None

    def test_relay_url_only(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://localhost:4222"}}
        _, url, auth = extract_biff_fields(raw)
        assert url == "nats://localhost:4222"
        assert auth is None

    def test_token_auth(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": "s3cret"}}
        _, _, auth = extract_biff_fields(raw)
        assert auth == RelayAuth(token="s3cret")

    def test_nkeys_seed_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "nkeys_seed": "/path/to.nk"}
        }
        _, _, auth = extract_biff_fields(raw)
        assert auth == RelayAuth(nkeys_seed="/path/to.nk")

    def test_user_credentials_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "user_credentials": "/path/to.creds"}
        }
        _, _, auth = extract_biff_fields(raw)
        assert auth == RelayAuth(user_credentials="/path/to.creds")

    def test_mutual_exclusivity_exits(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "nats://host", "token": "x", "nkeys_seed": "/y"}
        }
        with pytest.raises(SystemExit, match="Conflicting auth"):
            extract_biff_fields(raw)

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
            extract_biff_fields(raw)

    def test_empty_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": ""}}
        _, _, auth = extract_biff_fields(raw)
        assert auth is None

    def test_non_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": 42}}
        _, _, auth = extract_biff_fields(raw)
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

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_uses_remote_slug(
        self, _mock_gh: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.repo_name == "punt-labs__biff"

    @patch("biff.config.get_repo_slug", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_falls_back_to_dirname(
        self, _mock_gh: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.repo_name == sanitize_repo_name(repo.name)

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
    def test_no_repo_exits(self, _mock: object, tmp_path: Path) -> None:
        # No .git directory — must error, not silently fall back
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_config(start=tmp_path)

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_biff_file(self, _mock: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.team == ()
        assert resolved.config.relay_url is None

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_repo_exits_even_with_data_dir_override(
        self, _mock: object, tmp_path: Path
    ) -> None:
        custom = tmp_path / "data"
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_config(start=tmp_path, data_dir_override=custom)

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_relay_auth_flows_through(self, _mock: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        (tmp_path / ".biff").write_text(
            '[relay]\nurl = "tls://host"\nuser_credentials = "/creds"\n'
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_auth == RelayAuth(user_credentials="/creds")

    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_relay_url_override_clears_auth(
        self, _mock: object, tmp_path: Path
    ) -> None:
        """Overriding relay URL must clear .biff auth to prevent credential leak."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".biff").write_text(
            '[relay]\nurl = "tls://demo.example"\nuser_credentials = "/demo.creds"\n'
        )
        resolved = load_config(start=tmp_path, relay_url_override="tls://other.example")
        assert resolved.config.relay_url == "tls://other.example"
        assert resolved.config.relay_auth is None

    @patch("biff.config.get_github_identity", return_value=_KAI_NO_NAME)
    def test_empty_display_name_when_github_has_none(
        self, _mock: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == ""
