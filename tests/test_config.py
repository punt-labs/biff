"""Tests for configuration discovery and loading."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff._stdlib import _parse_repo_slug
from biff.config import (
    DEMO_RELAY_URL,
    EthosIdentity,
    EthosRoster,
    GitHubIdentity,
    compute_data_dir,
    extract_biff_fields,
    find_git_root,
    get_ethos_identity,
    get_ethos_roster,
    get_ethos_team,
    get_github_identity,
    get_os_user,
    get_repo_slug,
    load_config,
    resolve_agent_identity_from_disk,
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
        # Patch exists() so the parent walk never finds a .git directory.
        # Without this, TMPDIR inside a git repo (e.g. .tmp/) causes the
        # walk to find the host repo's .git.
        orig_exists = Path.exists

        def _no_git_exists(self: Path) -> bool:
            if self.name == ".git":
                return False
            return orig_exists(self)

        with patch.object(Path, "exists", _no_git_exists):
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


# -- extract_biff_fields (auth) --


class TestExtractRelayAuth:
    def test_no_relay_section(self) -> None:
        _, _, auth, _, _ = extract_biff_fields({})
        assert auth is None

    def test_relay_url_only(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://localhost:4222"}}
        _, url, auth, _, _ = extract_biff_fields(raw)
        assert url == "nats://localhost:4222"
        assert auth is None

    def test_token_auth(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": "s3cret"}}
        _, _, auth, _, _ = extract_biff_fields(raw)
        assert auth == RelayAuth(token="s3cret")

    def test_nkeys_seed_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "nkeys_seed": "/path/to.nk"}
        }
        _, _, auth, _, _ = extract_biff_fields(raw)
        assert auth == RelayAuth(nkeys_seed="/path/to.nk")

    def test_user_credentials_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "user_credentials": "/path/to.creds"}
        }
        _, _, auth, _, _ = extract_biff_fields(raw)
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
        _, _, auth, _, _ = extract_biff_fields(raw)
        assert auth is None

    def test_non_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": 42}}
        _, _, auth, _, _ = extract_biff_fields(raw)
        assert auth is None


# -- load_config --


_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")
_KAI_NO_NAME = GitHubIdentity(login="kai", display_name="")
_FROM_GIT = GitHubIdentity(login="from-git", display_name="Git User")


class TestLoadConfig:
    def _setup_repo(self, tmp_path: Path) -> Path:
        """Create a minimal git repo with YAML config."""
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "team:\n  members:\n    - kai\n    - eric\n"
            "relay:\n  url: nats://localhost:4222\n"
        )
        return tmp_path

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_uses_remote_slug(
        self, _mock_gh: object, _mock_ethos: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.repo_name == "punt-labs__biff"

    @patch("biff.config.get_repo_slug", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_falls_back_to_dirname(
        self, _mock_gh: object, _mock_ethos: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.repo_name == sanitize_repo_name(repo.name)

    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_full_discovery(
        self, _mock_gh: object, _mock_ethos: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == "Kai Chen"
        assert resolved.config.team == ("kai", "eric")
        assert resolved.config.relay_url == "nats://localhost:4222"
        assert resolved.data_dir == Path("/tmp/biff") / repo.name
        assert resolved.repo_root == repo

    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_custom_prefix(
        self, _mock_gh: object, _mock_ethos: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo, prefix=Path("/var/spool"))
        assert resolved.data_dir == Path("/var/spool/biff") / repo.name

    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_data_dir_override(
        self, _mock_gh: object, _mock_ethos: object, tmp_path: Path
    ) -> None:
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
    @patch("biff.config.get_ethos_identity", return_value=None)
    def test_falls_back_to_os_user(
        self, _mock_ethos: object, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""

    @patch("biff.config.get_os_user", return_value=None)
    @patch("biff.config.get_github_identity", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    def test_exits_when_all_user_sources_fail(
        self, _mock_ethos: object, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        with pytest.raises(SystemExit, match="No user configured"):
            load_config(start=tmp_path)

    @patch("biff.config.find_git_root", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_repo_exits(
        self, _mock: object, _mock_root: object, tmp_path: Path
    ) -> None:
        # No .git directory — must error, not silently fall back
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_config(start=tmp_path)

    @patch("biff.config.get_ethos_team", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_zero_config_uses_demo_relay(
        self,
        _mock_gh: object,
        _mock_ethos: object,
        _mock_team: object,
        tmp_path: Path,
    ) -> None:
        """Zero-config mode: no config.yaml -> demo relay."""
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.team == ()
        assert resolved.config.relay_url == DEMO_RELAY_URL
        assert resolved.config.relay_auth is not None

    @patch("biff.config.find_git_root", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_no_repo_exits_even_with_data_dir_override(
        self, _mock: object, _mock_root: object, tmp_path: Path
    ) -> None:
        custom = tmp_path / "data"
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_config(start=tmp_path, data_dir_override=custom)

    @patch("biff.config.get_ethos_team", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_relay_auth_flows_through(
        self, _mock_gh: object, _mock_ethos: object, _mock_team: object, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: tls://host\n  auth:\n    user_credentials: /creds\n"
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_auth == RelayAuth(user_credentials="/creds")

    @patch("biff.config.get_ethos_team", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_relay_url_override_clears_auth(
        self, _mock_gh: object, _mock_ethos: object, _mock_team: object, tmp_path: Path
    ) -> None:
        """Overriding relay URL must clear config auth to prevent credential leak."""
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: tls://demo.example\n"
            "  auth:\n    user_credentials: /demo.creds\n"
        )
        resolved = load_config(start=tmp_path, relay_url_override="tls://other.example")
        assert resolved.config.relay_url == "tls://other.example"
        assert resolved.config.relay_auth is None

    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI_NO_NAME)
    def test_empty_display_name_when_github_has_none(
        self, _mock_gh: object, _mock_ethos: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == ""

    @patch(
        "biff.config.get_ethos_identity",
        return_value=EthosIdentity(
            handle="claude", display_name="Claude Agento", kind="agent"
        ),
    )
    def test_ethos_identity_takes_precedence_over_github(
        self, _mock_ethos: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "claude"
        assert resolved.config.display_name == "Claude Agento"
        assert resolved.config.kind == "agent"

    @patch("biff.config.get_github_identity", return_value=_KAI)
    @patch("biff.config.get_ethos_identity", return_value=None)
    def test_ethos_fallback_to_github(
        self, _mock_ethos: object, _mock_gh: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.kind == ""

    @patch("biff.config.get_os_user", return_value="jfreeman")
    @patch("biff.config.get_github_identity", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=None)
    def test_ethos_absent_falls_through_to_os(
        self, _mock_ethos: object, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        self._setup_repo(tmp_path)
        resolved = load_config(start=tmp_path)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.kind == ""


# -- get_ethos_identity --


class TestGetEthosIdentity:
    def test_success(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"handle":"claude","name":"Claude Agento",'
                '"kind":"agent","github":"claude-puntlabs"}'
            )
            result = get_ethos_identity()
            assert result == EthosIdentity(
                handle="claude",
                display_name="Claude Agento",
                kind="agent",
            )

    def test_not_installed(self) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_ethos_identity() is None

    def test_exit_1(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_ethos_identity() is None

    def test_timeout(self) -> None:
        with patch(
            "biff.config.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ethos", timeout=2),
        ):
            assert get_ethos_identity() is None

    def test_bad_json(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            assert get_ethos_identity() is None

    def test_missing_handle(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = '{"name":"Claude","kind":"agent"}'
            assert get_ethos_identity() is None

    def test_empty_handle(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"handle":"","name":"Claude","kind":"agent"}'
            )
            assert get_ethos_identity() is None

    def test_empty_name_uses_handle(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"handle":"claude","name":"","kind":"agent"}'
            )
            result = get_ethos_identity()
            assert result is not None
            assert result.display_name == "claude"


# -- get_ethos_team --


class TestGetEthosTeam:
    def test_success(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '[{"name":"eng","members":['
                '{"identity":"claude","role":"coo"},'
                '{"identity":"jmf","role":"founder"}'
                "]}]"
            )
            result = get_ethos_team()
            assert result == ("claude", "jmf")

    def test_empty_array(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "[]"
            assert get_ethos_team() is None

    def test_not_installed(self) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_ethos_team() is None

    def test_multi_team_member_union(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                "["
                '{"name":"eng","members":[{"identity":"claude"},{"identity":"jmf"}]},'
                '{"name":"ops","members":[{"identity":"adb"},{"identity":"jmf"}]}'
                "]"
            )
            result = get_ethos_team()
            assert result == ("adb", "claude", "jmf")


# -- get_ethos_roster --


_ROSTER_JSON = (
    '{"root":{"handle":"jfreeman","kind":"human","display_name":"Jim Freeman"},'
    '"primary":{"handle":"claude","kind":"agent","display_name":"Claude Agento"}}'
)

_ROSTER_SAME = (
    '{"root":{"handle":"claude","kind":"agent","display_name":"Claude Agento"},'
    '"primary":{"handle":"claude","kind":"agent","display_name":"Claude Agento"}}'
)


class TestGetEthosRoster:
    def test_success(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = _ROSTER_JSON
            result = get_ethos_roster()
            assert result is not None
            assert result.root == EthosIdentity(
                handle="jfreeman", display_name="Jim Freeman", kind="human"
            )
            assert result.primary == EthosIdentity(
                handle="claude", display_name="Claude Agento", kind="agent"
            )

    def test_not_installed(self) -> None:
        with patch("biff.config.subprocess.run", side_effect=FileNotFoundError):
            assert get_ethos_roster() is None

    def test_exit_1(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 1
            mock_run.return_value.stdout = ""
            assert get_ethos_roster() is None

    def test_timeout(self) -> None:
        with patch(
            "biff.config.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="ethos", timeout=2),
        ):
            assert get_ethos_roster() is None

    def test_bad_json(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "not json"
            assert get_ethos_roster() is None

    def test_missing_root(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"primary":{"handle":"claude","kind":"agent",'
                '"display_name":"Claude Agento"}}'
            )
            result = get_ethos_roster()
            assert result is not None
            assert result.root is None
            assert result.primary is not None

    def test_missing_primary(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"root":{"handle":"jfreeman","kind":"human",'
                '"display_name":"Jim Freeman"}}'
            )
            result = get_ethos_roster()
            assert result is not None
            assert result.root is not None
            assert result.primary is None

    def test_empty_handle_in_entry(self) -> None:
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = (
                '{"root":{"handle":"","kind":"human","display_name":"X"},'
                '"primary":{"handle":"claude","kind":"agent",'
                '"display_name":"Claude"}}'
            )
            result = get_ethos_roster()
            assert result is not None
            assert result.root is None  # empty handle → None

    def test_participants_format(self) -> None:
        """Current ethos roster format: participants array."""
        roster_json = (
            '{"session":"abc","participants":['
            '{"agent_id":"jfreeman","persona":"jfreeman"},'
            '{"agent_id":"12345","persona":"claude","parent":"jfreeman"}'
            "]}"
        )
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = roster_json
            result = get_ethos_roster()
            assert result is not None
            assert result.root is not None
            assert result.root.handle == "jfreeman"
            assert result.primary is not None
            assert result.primary.handle == "claude"

    def test_participants_no_parent_means_root(self) -> None:
        """Entry without parent is root."""
        roster_json = '{"participants":[{"agent_id":"kai","persona":"kai"}]}'
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = roster_json
            result = get_ethos_roster()
            assert result is not None
            assert result.root is not None
            assert result.root.handle == "kai"
            assert result.primary is None

    def test_participants_single_agent_only(self) -> None:
        """Only an agent (with parent) and no root."""
        roster_json = (
            '{"participants":[{"agent_id":"99","persona":"claude","parent":"unknown"}]}'
        )
        with patch("biff.config.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = roster_json
            result = get_ethos_roster()
            assert result is not None
            assert result.root is None
            assert result.primary is not None
            assert result.primary.handle == "claude"


# -- load_config with dual-session roster --


_CLAUDE_ETHOS = EthosIdentity(
    handle="claude", display_name="Claude Agento", kind="agent"
)

_DUAL_ROSTER = EthosRoster(
    root=EthosIdentity(handle="jfreeman", display_name="Jim Freeman", kind="human"),
    primary=_CLAUDE_ETHOS,
)

_SAME_ROSTER = EthosRoster(root=_CLAUDE_ETHOS, primary=_CLAUDE_ETHOS)


class TestLoadConfigDualSession:
    def _setup_repo(self, tmp_path: Path) -> Path:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "team:\n  members:\n    - kai\nrelay:\n  url: nats://localhost:4222\n"
        )
        return tmp_path

    @patch("biff.config.get_ethos_roster", return_value=_DUAL_ROSTER)
    @patch("biff.config.get_ethos_identity", return_value=_CLAUDE_ETHOS)
    def test_dual_session_sets_root_identity(
        self, _mock_ethos: object, _mock_roster: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.config.user == "claude"
        assert resolved.root_identity is not None
        assert resolved.root_identity.handle == "jfreeman"
        assert resolved.root_identity.kind == "human"

    @patch("biff.config.get_ethos_roster", return_value=_SAME_ROSTER)
    @patch("biff.config.get_ethos_identity", return_value=_CLAUDE_ETHOS)
    def test_same_identity_no_root(
        self, _mock_ethos: object, _mock_roster: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.root_identity is None

    @patch("biff.config.get_ethos_roster", return_value=None)
    @patch("biff.config.get_ethos_identity", return_value=_CLAUDE_ETHOS)
    def test_roster_absent_no_root(
        self, _mock_ethos: object, _mock_roster: object, tmp_path: Path
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo)
        assert resolved.root_identity is None

    @patch("biff.config.get_ethos_roster", return_value=_DUAL_ROSTER)
    @patch("biff.config.get_ethos_identity", return_value=None)
    @patch(
        "biff.config.get_github_identity",
        return_value=GitHubIdentity(login="kai", display_name="Kai"),
    )
    def test_user_override_skips_roster(
        self,
        _mock_gh: object,
        _mock_ethos: object,
        _mock_roster: object,
        tmp_path: Path,
    ) -> None:
        repo = self._setup_repo(tmp_path)
        resolved = load_config(start=repo, user_override="custom")
        assert resolved.root_identity is None


# -- resolve_agent_identity_from_disk --


def _write_ethos_yaml(repo_root: Path, body: str) -> Path:
    """Write ``.punt-labs/ethos.yaml`` under *repo_root* and return its path."""
    ethos_dir = repo_root / ".punt-labs"
    ethos_dir.mkdir(parents=True, exist_ok=True)
    path = ethos_dir / "ethos.yaml"
    path.write_text(body)
    return path


def _write_identity_yaml(repo_root: Path, handle: str, body: str) -> Path:
    """Write ``.punt-labs/ethos/identities/{handle}.yaml`` and return its path."""
    identities = repo_root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    path = identities / f"{handle}.yaml"
    path.write_text(body)
    return path


class TestResolveAgentIdentityFromDisk:
    def test_happy_path(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(
            tmp_path,
            "claude",
            "handle: claude\nname: Claude Agento\nkind: agent\n",
        )
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result == EthosIdentity(
            handle="claude", display_name="Claude Agento", kind="agent"
        )

    def test_legacy_config_yaml_location(self, tmp_path: Path) -> None:
        """Legacy fallback: ``.punt-labs/ethos/config.yaml`` is read when ``ethos.yaml`` is absent."""  # noqa: E501
        legacy = tmp_path / ".punt-labs" / "ethos"
        legacy.mkdir(parents=True)
        (legacy / "config.yaml").write_text("agent: claude\n")
        _write_identity_yaml(
            tmp_path,
            "claude",
            "handle: claude\nname: Claude Agento\nkind: agent\n",
        )
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.handle == "claude"

    def test_missing_ethos_yaml(self, tmp_path: Path) -> None:
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_missing_identity_yaml(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_malformed_ethos_yaml(self, tmp_path: Path) -> None:
        # Unclosed bracket forces yaml.YAMLError.
        _write_ethos_yaml(tmp_path, "agent: [unclosed\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_malformed_identity_yaml(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "kind: [agent\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_missing_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "team: engineering\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_empty_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: ''\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_whitespace_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: '   '\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_non_string_agent_field(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: 42\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_kind_human_rejected(self, tmp_path: Path) -> None:
        """``kind: human`` must NOT be elevated into the agent slot."""
        _write_ethos_yaml(tmp_path, "agent: jfreeman\n")
        _write_identity_yaml(
            tmp_path,
            "jfreeman",
            "handle: jfreeman\nname: Jim Freeman\nkind: human\n",
        )
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_kind_missing_rejected(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "handle: claude\nname: Claude\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    @pytest.mark.parametrize(
        "bad_handle",
        [
            "../../etc/passwd",
            "../foo",
            "foo/bar",
            ".hidden",
            "Claude",  # uppercase
            "claude!",  # special char
            "a" * 65,  # too long
            "-leading-hyphen",
            "_leading-underscore",
        ],
    )
    def test_path_traversal_and_invalid_handles_rejected(
        self, tmp_path: Path, bad_handle: str
    ) -> None:
        """The handle regex blocks every path-traversal payload before any FS read."""
        _write_ethos_yaml(tmp_path, f"agent: {bad_handle!r}\n")
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_handle_outside_identities_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A handle that passes the regex but resolves outside identities/ is rejected.

        The regex blocks ``/``, ``..``, etc. -- to exercise the
        ``is_relative_to`` guard we replace ``Path.resolve`` so the
        resolved identity path falls outside the identities directory.
        """
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(
            tmp_path, "claude", "handle: claude\nname: Claude\nkind: agent\n"
        )

        identities_root = (tmp_path / ".punt-labs" / "ethos" / "identities").resolve()
        outside = tmp_path / "outside.yaml"
        outside.write_text("handle: claude\nname: Claude\nkind: agent\n")

        original_resolve = Path.resolve

        def fake_resolve(self: Path, *args: object, **kwargs: object) -> Path:
            if self.name == "claude.yaml" and self.parent == identities_root:
                return outside
            return original_resolve(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "resolve", fake_resolve)
        assert resolve_agent_identity_from_disk(tmp_path) is None

    def test_handle_falls_back_to_agent_field(self, tmp_path: Path) -> None:
        """When the identity YAML omits ``handle``, fall back to the agent field."""
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "name: Claude Agento\nkind: agent\n")
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.handle == "claude"

    def test_display_name_falls_back_to_handle(self, tmp_path: Path) -> None:
        _write_ethos_yaml(tmp_path, "agent: claude\n")
        _write_identity_yaml(tmp_path, "claude", "handle: claude\nkind: agent\n")
        result = resolve_agent_identity_from_disk(tmp_path)
        assert result is not None
        assert result.display_name == "claude"
