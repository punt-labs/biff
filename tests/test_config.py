"""Tests for configuration discovery and loading."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from biff._stdlib import _parse_repo_slug
from biff.config import (
    _apply_env_relay_overrides,
    _ConfigFields,
    _resolve_relay_fields,
    _tls_env_override,
    find_git_root,
    get_repo_slug,
    load_cli_config,
    load_mcp_config,
    sanitize_repo_name,
)
from biff.identity import GitHubIdentity
from biff.models import RelayAuth

from ._config_fields import extract_biff_fields
from ._identity_fixtures import write_gitmodules_ethos

_CONFIG_LOGGER = "biff.config"

# chmod-based permission-denial tests don't work when running as root
# (root bypasses the permission bits entirely).
_SKIP_AS_ROOT = pytest.mark.skipif(
    os.geteuid() == 0, reason="permission checks are meaningless as root"
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

    @pytest.mark.parametrize("suffix", ["", ".git"])
    def test_devin_github_proxy(self, suffix: str) -> None:
        url = f"https://git-manager.devin.ai/proxy/github.com/punt-labs/biff{suffix}"
        assert _parse_repo_slug(url) == "punt-labs/biff"

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com/proxy/github.com/punt-labs/biff",
            "https://git-manager.devin.ai/proxy/gitlab.com/group/repo",
            "https://git-manager.devin.ai/proxy/github.com",
            "http://git-manager.devin.ai/proxy/github.com",
            "ssh://git@GIT-MANAGER.DEVIN.AI/proxy/github.com",
            "https://git-manager.devin.ai:443/proxy/github.com",
            "https://git-manager.devin.ai/proxy/github.com//biff",
            "https://git-manager.devin.ai/proxy/github.com/punt-labs/",
            "https://git-manager.devin.ai/proxy/github.com/group/sub/repo",
        ],
    )
    def test_invalid_proxy_route_rejected(self, url: str) -> None:
        assert _parse_repo_slug(url) is None

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
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 0
            mock_run.return_value.stdout = "git@github.com:punt-labs/biff.git\n"
            assert get_repo_slug(tmp_path) == "punt-labs/biff"

    def test_no_remote_returns_none(self, tmp_path: Path) -> None:
        with patch("biff._stdlib.subprocess.run") as mock_run:
            mock_run.return_value.returncode = 128
            mock_run.return_value.stdout = ""
            assert get_repo_slug(tmp_path) is None

    def test_git_not_found_returns_none(self, tmp_path: Path) -> None:
        with patch("biff._stdlib.subprocess.run", side_effect=FileNotFoundError):
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


# -- extract_biff_fields (auth) --


class TestExtractRelayAuth:
    def test_no_relay_section(self) -> None:
        _, _, auth, _, _, _ = extract_biff_fields({})
        assert auth is None

    def test_relay_url_only(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://localhost:4222"}}
        _, url, auth, _, _, _ = extract_biff_fields(raw)
        assert url == "nats://localhost:4222"
        assert auth is None

    def test_token_auth(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": "s3cret"}}
        _, _, auth, _, _, _ = extract_biff_fields(raw)
        assert auth == RelayAuth(token="s3cret")

    def test_nkeys_seed_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "nkeys_seed": "/path/to.nk"}
        }
        _, _, auth, _, _, _ = extract_biff_fields(raw)
        assert auth == RelayAuth(nkeys_seed="/path/to.nk")

    def test_user_credentials_auth(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "user_credentials": "/path/to.creds"}
        }
        _, _, auth, _, _, _ = extract_biff_fields(raw)
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
        _, _, auth, _, _, _ = extract_biff_fields(raw)
        assert auth is None

    def test_non_string_auth_ignored(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "nats://host", "token": 42}}
        _, _, auth, _, _, _ = extract_biff_fields(raw)
        assert auth is None


class TestExtractRelayTlsHandshakeFirst:
    def test_no_relay_section_defaults_false(self) -> None:
        _, _, _, tls_handshake_first, _, _ = extract_biff_fields({})
        assert tls_handshake_first is False

    def test_absent_defaults_false(self) -> None:
        raw: dict[str, object] = {"relay": {"url": "tls://host"}}
        _, _, _, tls_handshake_first, _, _ = extract_biff_fields(raw)
        assert tls_handshake_first is False

    def test_explicit_true(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "tls_handshake_first": True}
        }
        _, _, _, tls_handshake_first, _, _ = extract_biff_fields(raw)
        assert tls_handshake_first is True

    def test_explicit_false(self) -> None:
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "tls_handshake_first": False}
        }
        _, _, _, tls_handshake_first, _, _ = extract_biff_fields(raw)
        assert tls_handshake_first is False

    def test_non_bool_value_is_an_error(self) -> None:
        # A stray string/int in the YAML (typo, wrong type) must not be
        # silently read as "off".
        raw: dict[str, object] = {
            "relay": {"url": "tls://host", "tls_handshake_first": "true"}
        }
        with pytest.raises(SystemExit, match="tls_handshake_first must be true"):
            extract_biff_fields(raw)


# -- load_mcp_config / load_cli_config --


_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")
_KAI_NO_NAME = GitHubIdentity(login="kai", display_name="")
_FROM_GIT = GitHubIdentity(login="from-git", display_name="Git User")
_BOT_IDENTITY = GitHubIdentity(login="claude-puntlabs", display_name="Claude Agento")


def _setup_repo_with_yaml(tmp_path: Path) -> Path:
    """Create a minimal git repo with biff config.yaml. Returns repo root."""
    (tmp_path / ".git").mkdir()
    biff_dir = tmp_path / ".punt-labs" / "biff"
    biff_dir.mkdir(parents=True)
    (biff_dir / "config.yaml").write_text(
        "team:\n  members:\n    - kai\n    - eric\n"
        "relay:\n  url: nats://localhost:4222\n"
    )
    return tmp_path


def _write_agent_identity_fixture(repo_root: Path, handle: str = "claude") -> None:
    """Write ethos.yaml + matching agent identity YAML to *repo_root*."""
    ethos_dir = repo_root / ".punt-labs"
    ethos_dir.mkdir(parents=True, exist_ok=True)
    (ethos_dir / "ethos.yaml").write_text(f"agent: {handle}\n")
    identities = repo_root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    (identities / f"{handle}.yaml").write_text(
        f"handle: {handle}\nname: Claude Agento\nkind: agent\n"
    )


def _write_bot_identity_fixture(repo_root: Path, handle: str = "claude") -> None:
    """Register a ``kind: agent`` identity with a GitHub login, no ``ethos.yaml``.

    Unlike :func:`_write_agent_identity_fixture`, this omits
    ``.punt-labs/ethos.yaml`` -- the bot-login cross-check scans every
    identity file directly and must not depend on the repo's *active*
    agent being configured.
    """
    identities = repo_root / ".punt-labs" / "ethos" / "identities"
    identities.mkdir(parents=True, exist_ok=True)
    (identities / f"{handle}.yaml").write_text(
        f"handle: {handle}\nname: Claude Agento\nkind: agent\ngithub: claude-puntlabs\n"
    )


class TestLoadCliConfig:
    """The CLI entry point uses the human-identity chain only."""

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_uses_remote_slug(
        self, _mock_gh: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.repo_name == "punt-labs__biff"

    @patch("biff.identity.get_github_identity")
    @patch("biff._stdlib.subprocess.run")
    def test_devin_proxy_keeps_repo_org_with_explicit_identity(
        self, run: MagicMock, github_identity: MagicMock, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        run.return_value.returncode = 0
        run.return_value.stdout = (
            "https://git-manager.devin.ai/proxy/github.com/punt-labs/vox\n"
        )
        with patch("biff.config.get_repo_common_root", return_value=str(repo)):
            resolved = load_cli_config(start=repo, user_override="devin")
        assert resolved.config.user == "devin"
        assert resolved.config.repo_name == "punt-labs__vox"
        assert resolved.config.orgs == ("punt-labs",)
        github_identity.assert_not_called()

    @patch("biff.config.get_repo_slug", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_falls_back_to_dirname(
        self, _mock_gh: object, _mock_slug: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=repo)
        assert resolved.config.repo_name == sanitize_repo_name(repo.name)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_full_discovery(self, _mock_gh: object, tmp_path: Path) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == "Kai Chen"
        assert resolved.config.team == ("kai", "eric")
        assert resolved.config.relay_url == "nats://localhost:4222"
        assert resolved.data_dir == (
            repo.resolve() / ".punt-labs" / "local" / "biff" / "relay"
        )
        assert resolved.repo_root == repo

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_data_dir_override(self, _mock_gh: object, tmp_path: Path) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        custom = tmp_path / "custom"
        resolved = load_cli_config(start=repo, data_dir_override=custom)
        assert resolved.data_dir == custom

    @patch("biff.identity.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_takes_precedence(
        self, _mock: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="from-cli")
        assert resolved.config.user == "from-cli"

    @patch("biff.identity.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_clears_display_name(
        self, _mock: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="from-cli")
        assert resolved.config.display_name == ""

    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_falls_back_to_os_user(
        self, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""

    @patch("biff.identity.get_os_user", return_value="jim")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_launched_by_is_the_os_user_not_the_handle(
        self, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        # No ethos data, no config key: the launching user is the OS user,
        # even when the session's handle comes from the CLI override.
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="claude")
        assert resolved.config.user == "claude"
        assert resolved.config.launched_by == "jim"

    @patch("biff.identity.git_user_name", return_value="jmf-pobox")
    @patch("biff.identity.get_os_user", return_value="jim")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_owner_git_is_captured_into_the_config(
        self,
        _mock_git: object,
        _mock_os: object,
        _mock_name: object,
        tmp_path: Path,
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="claude")
        assert resolved.config.owner_git == "jmf-pobox"

    @patch("biff.identity.git_user_name", return_value="Devin AI")
    @patch("biff.identity.get_os_user", return_value="ubuntu")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_biff_owner_names_the_owner_over_git_user_name(
        self,
        _mock_git: object,
        _mock_os: object,
        _mock_name: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # A hosted agent's git user.name is the bot; BIFF_OWNER names its human.
        monkeypatch.setenv("BIFF_OWNER", "jmf-pobox")
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="devin")
        assert resolved.config.owner_git == "jmf-pobox"

    @patch("biff.identity.git_user_name", return_value="jmf-pobox")
    @patch("biff.identity.get_os_user", return_value="jim")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_empty_biff_owner_falls_back_to_git_user_name(
        self,
        _mock_git: object,
        _mock_os: object,
        _mock_name: object,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("BIFF_OWNER", "")
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path, user_override="claude")
        assert resolved.config.owner_git == "jmf-pobox"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_user_override_naming_an_agent_identity_is_that_agent(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo, handle="devin")
        resolved = load_cli_config(start=repo, user_override="devin")
        assert resolved.config.user == "devin"
        assert resolved.config.kind == "agent"

    @patch("biff.identity.get_os_user", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_exits_when_all_user_sources_fail(
        self, _mock_git: object, _mock_os: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        with pytest.raises(SystemExit, match="No user configured"):
            load_cli_config(start=tmp_path)

    @patch("biff.config.find_git_root", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_no_repo_exits(
        self, _mock: object, _mock_root: object, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_cli_config(start=tmp_path)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_zero_config_uses_local_relay(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.team == ()
        assert resolved.config.relay_url is None
        assert resolved.config.relay_auth is None

    @patch("biff.config.find_git_root", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_no_repo_exits_even_with_data_dir_override(
        self, _mock: object, _mock_root: object, tmp_path: Path
    ) -> None:
        custom = tmp_path / "data"
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_cli_config(start=tmp_path, data_dir_override=custom)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_relay_auth_flows_through(self, _mock_gh: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: tls://host\n  auth:\n    user_credentials: /creds\n"
        )
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_auth == RelayAuth(user_credentials="/creds")

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_relay_tls_handshake_first_flows_through(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """Regression: this value passes through several dataclass
        reconstructions in config.py (_ConfigFields, _BaseConfig,
        BiffConfig) that each re-list fields by hand -- a dropped field
        in any of them would silently reset to the False default and
        this test would catch it where the unit-level extract_biff_fields
        tests (which stop before any of that reconstruction) cannot.
        """
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n"
            "  url: tls://proxy.example:4222\n"
            "  auth:\n    token: s3cret\n"
            "  tls_handshake_first: true\n"
        )
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_tls_handshake_first is True

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_relay_url_override_clears_auth(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """Overriding relay URL must clear config auth to prevent credential leak."""
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: tls://demo.example\n"
            "  auth:\n    user_credentials: /demo.creds\n"
        )
        resolved = load_cli_config(
            start=tmp_path, relay_url_override="tls://other.example"
        )
        assert resolved.config.relay_url == "tls://other.example"
        assert resolved.config.relay_auth is None

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_relay_url_override_clears_tls_handshake_first(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """Overriding relay URL must clear tls_handshake_first too.

        It's a property of the relay being replaced (e.g. a
        TLS-terminating proxy), not the override target -- carrying it
        over could force it onto a native-TLS server (the demo relay)
        that must never receive it.
        """
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n"
            "  url: tls://proxy.example:4222\n"
            "  auth:\n    token: s3cret\n"
            "  tls_handshake_first: true\n"
        )
        resolved = load_cli_config(
            start=tmp_path, relay_url_override="tls://connect.ngs.global"
        )
        assert resolved.config.relay_url == "tls://connect.ngs.global"
        assert resolved.config.relay_tls_handshake_first is False

    @patch("biff.identity.get_github_identity", return_value=_KAI_NO_NAME)
    def test_empty_display_name_when_github_has_none(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == ""

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_ignores_disk_agent_identity(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """The CLI does NOT read .punt-labs/ethos.yaml -- humans identify as themselves.

        Regression for spec § 1.1 (R5): if the CLI silently used the
        agent identity, a human's ``biff write`` would carry
        ``FROM=agent``.
        """
        repo = _setup_repo_with_yaml(tmp_path)
        _write_agent_identity_fixture(repo)
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "kai"
        assert resolved.config.kind == ""

    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_rejects_bot_github_login_falls_back_to_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        """A ``GH_TOKEN`` pinned to a bot PAT must not silently become the CLI user.

        Every Claude Agento session sources a
        bot ``GH_TOKEN`` into its shell (org CLAUDE.md). Once ``gh api
        user`` resolves to that bot, the CLI must reject it and fall
        back to the OS user rather than silently identifying the human
        operator as the bot.
        """
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo)
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""
        assert resolved.config.kind == ""

    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_rejects_bot_github_login_differing_only_by_case(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        """GitHub logins are case-insensitive -- a registry entry that
        differs only by case (or trailing whitespace) from the resolved
        login must still be caught by the bot denylist, not fail open.
        """
        repo = _setup_repo_with_yaml(tmp_path)
        identities = repo / ".punt-labs" / "ethos" / "identities"
        identities.mkdir(parents=True, exist_ok=True)
        (identities / "claude.yaml").write_text(
            "handle: claude\nname: Claude Agento\nkind: agent\n"
            "github: ' Claude-PuntLabs '\n"
        )
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.kind == ""

    @patch("biff.identity.get_os_user", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_rejects_bot_github_login_exits_when_no_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo)
        with pytest.raises(SystemExit, match="claude-puntlabs"):
            load_cli_config(start=repo)

    @_SKIP_AS_ROOT
    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_corrupted_bot_identity_file_falls_back_with_warning(
        self,
        _mock_gh: object,
        _mock_os: object,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Fall back to the OS user with a warning for unreadable agent identities.

        An unreadable registration cannot prove the GitHub login belongs to a human.
        """
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo)
        identity_path = repo / ".punt-labs" / "ethos" / "identities" / "claude.yaml"
        identity_path.chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger=_CONFIG_LOGGER):
                resolved = load_cli_config(start=repo)
        finally:
            identity_path.chmod(0o644)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.kind == ""
        assert any(
            "Cannot confirm GitHub login" in r.getMessage() for r in caplog.records
        )
        assert any(
            "Failed to read identity YAML" in r.getMessage() for r in caplog.records
        )

    @_SKIP_AS_ROOT
    @patch("biff.identity.get_os_user", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_corrupted_bot_identity_file_exits_when_no_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo)
        identity_path = repo / ".punt-labs" / "ethos" / "identities" / "claude.yaml"
        identity_path.chmod(0o000)
        try:
            with pytest.raises(SystemExit, match="Cannot confirm"):
                load_cli_config(start=repo)
        finally:
            identity_path.chmod(0o644)

    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_uninitialized_ethos_submodule_falls_back_to_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        """Reject unverifiable identities when a declared registry is absent.

        A missing identities directory is not proof that no agent logins exist.
        """
        repo = _setup_repo_with_yaml(tmp_path)
        write_gitmodules_ethos(repo)
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""
        assert resolved.config.kind == ""

    @patch("biff.identity.get_os_user", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_BOT_IDENTITY)
    def test_uninitialized_ethos_submodule_exits_when_no_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        write_gitmodules_ethos(repo)
        with pytest.raises(SystemExit, match="Cannot confirm"):
            load_cli_config(start=repo)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_uses_github_login_when_no_matching_bot_identity(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """A GitHub login with no matching agent identity is used normally."""
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo)  # registers claude-puntlabs, not kai
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "kai"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_human_identity_with_github_field_is_not_rejected(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        """Only ``kind: agent`` identities are cross-checked -- humans are exempt."""
        repo = _setup_repo_with_yaml(tmp_path)
        identities = repo / ".punt-labs" / "ethos" / "identities"
        identities.mkdir(parents=True, exist_ok=True)
        (identities / "jfreeman.yaml").write_text(
            "handle: jfreeman\nname: Jim Freeman\nkind: human\ngithub: kai\n"
        )
        resolved = load_cli_config(start=repo)
        assert resolved.config.user == "kai"


class TestLoadMcpConfig:
    """The MCP entry point uses the agent-first chain (disk -> GitHub -> OS)."""

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_disk_identity_takes_precedence_over_github(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        _write_agent_identity_fixture(repo)
        resolved = load_mcp_config(start=repo)
        assert resolved.config.user == "claude"
        assert resolved.config.display_name == "Claude Agento"
        assert resolved.config.kind == "agent"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_falls_back_to_github_when_no_disk_identity(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_mcp_config(start=tmp_path)
        assert resolved.config.user == "kai"
        assert resolved.config.kind == ""

    @patch("biff.identity.get_os_user", return_value="jfreeman")
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_falls_back_to_os_user(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        resolved = load_mcp_config(start=tmp_path)
        assert resolved.config.user == "jfreeman"
        assert resolved.config.display_name == ""

    @patch("biff.identity.get_os_user", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=None)
    def test_exits_when_all_user_sources_fail(
        self, _mock_gh: object, _mock_os: object, tmp_path: Path
    ) -> None:
        _setup_repo_with_yaml(tmp_path)
        with pytest.raises(SystemExit, match="No user configured"):
            load_mcp_config(start=tmp_path)

    @patch("biff.identity.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_short_circuits_disk(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        _write_agent_identity_fixture(repo)
        resolved = load_mcp_config(start=repo, user_override="from-cli")
        assert resolved.config.user == "from-cli"
        assert resolved.config.kind == ""

    @patch("biff.identity.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_naming_an_agent_identity_is_that_agent(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        # Devin launches ``biff mcp --user devin`` with no ethos.yaml; the
        # registered ``kind: agent`` identity still flags the session [A].
        repo = _setup_repo_with_yaml(tmp_path)
        _write_bot_identity_fixture(repo, handle="devin")
        resolved = load_mcp_config(start=repo, user_override="devin")
        assert resolved.config.user == "devin"
        assert resolved.config.display_name == "Claude Agento"
        assert resolved.config.kind == "agent"

    @patch("biff.identity.get_github_identity", return_value=_FROM_GIT)
    def test_user_override_naming_a_human_identity_stays_plain(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        repo = _setup_repo_with_yaml(tmp_path)
        identities = repo / ".punt-labs" / "ethos" / "identities"
        identities.mkdir(parents=True)
        (identities / "kai.yaml").write_text("handle: kai\nname: Kai\nkind: human\n")
        resolved = load_mcp_config(start=repo, user_override="kai")
        assert resolved.config.user == "kai"
        assert resolved.config.display_name == ""
        assert resolved.config.kind == ""

    @patch("biff.config.find_git_root", return_value=None)
    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_no_repo_exits(
        self, _mock: object, _mock_root: object, tmp_path: Path
    ) -> None:
        with pytest.raises(SystemExit, match="Not in a git repository"):
            load_mcp_config(start=tmp_path)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_zero_config_uses_local_relay(
        self, _mock_gh: object, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_mcp_config(start=tmp_path)
        assert resolved.config.relay_url is None
        assert resolved.config.relay_auth is None


# -- _apply_env_relay_overrides --


class TestApplyEnvRelayOverrides:
    """Unit tests for the BIFF_RELAY_* env var override layer.

    Every test clears the five env vars first via ``monkeypatch.delenv``
    (``raising=False``) so ambient state from the developer's shell (or
    ``.envrc.local``, per docs/relay-env-overrides.md Sec 0) can never
    leak into the assertion.
    """

    _ENV_VARS = (
        "BIFF_RELAY_URL",
        "BIFF_RELAY_TOKEN",
        "BIFF_RELAY_NKEYS_SEED",
        "BIFF_RELAY_USER_CREDENTIALS",
        "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    )

    @pytest.fixture(autouse=True)
    def _clear_relay_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in self._ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    def test_no_env_vars_no_op(self) -> None:
        cf = _ConfigFields(relay_url="nats://file", relay_auth=RelayAuth(token="f"))
        assert _apply_env_relay_overrides(cf) == cf

    def test_url_env_var_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_url == "tls://from-env:4222"

    def test_token_env_var_applies(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "s3cret")
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_auth == RelayAuth(token="s3cret")

    def test_nkeys_seed_env_var_applies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        seed = tmp_path / "seed.nk"
        seed.write_text("seed")
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", str(seed))
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_auth == RelayAuth(nkeys_seed=str(seed))

    def test_user_credentials_env_var_applies(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        creds = tmp_path / "relay.creds"
        creds.write_text("creds")
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_USER_CREDENTIALS", str(creds))
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_auth == RelayAuth(user_credentials=str(creds))

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "Yes"])
    def test_tls_handshake_first_true_values(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_TLS_HANDSHAKE_FIRST", value)
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_tls_handshake_first is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "", "garbage"])
    def test_tls_handshake_first_non_true_values_ignored(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_TLS_HANDSHAKE_FIRST", value)
        cf = _apply_env_relay_overrides(_ConfigFields(relay_tls_handshake_first=False))
        assert cf.relay_tls_handshake_first is False

    def test_env_var_precedence_over_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An env var beats the file-resolved value when the gate is open."""
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        cf = _ConfigFields(relay_url=None)  # gate open: no file-resolved URL
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "tls://from-env:4222"

    def test_mutual_exclusivity_conflict_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "x")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", "/y")
        with pytest.raises(SystemExit, match="Conflicting relay auth env vars"):
            _apply_env_relay_overrides(_ConfigFields())

    def test_mutual_exclusivity_error_names_vars_not_values(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "super-secret-value")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", "/y")
        with pytest.raises(SystemExit) as exc_info:
            _apply_env_relay_overrides(_ConfigFields())
        message = str(exc_info.value)
        assert "BIFF_RELAY_TOKEN" in message
        assert "BIFF_RELAY_NKEYS_SEED" in message
        assert "super-secret-value" not in message

    def test_all_three_auth_vars_conflict_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "x")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", "/y")
        monkeypatch.setenv("BIFF_RELAY_USER_CREDENTIALS", "/z")
        with pytest.raises(SystemExit, match="Conflicting relay auth env vars"):
            _apply_env_relay_overrides(_ConfigFields())

    def test_env_auth_wholesale_replaces_file_auth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """One env auth var replaces the file's RelayAuth entirely, never merges."""
        seed = tmp_path / "seed.nk"
        seed.write_text("seed")
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", str(seed))
        cf = _ConfigFields(relay_url=None, relay_auth=RelayAuth(token="from-file"))
        result = _apply_env_relay_overrides(cf)
        assert result.relay_auth == RelayAuth(nkeys_seed=str(seed))
        assert result.relay_auth is not None
        assert result.relay_auth.token is None

    def test_no_auth_env_vars_falls_through_unchanged(self) -> None:
        file_auth = RelayAuth(token="from-file")
        cf = _ConfigFields(relay_url=None, relay_auth=file_auth)
        result = _apply_env_relay_overrides(cf)
        assert result.relay_auth == file_auth

    def test_url_alone_clears_auth_and_tls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        cf = _ConfigFields(
            relay_url=None,
            relay_auth=RelayAuth(token="from-file"),
            relay_tls_handshake_first=True,
        )
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "tls://from-env:4222"
        assert result.relay_auth is None
        assert result.relay_tls_handshake_first is False

    def test_url_with_auth_does_not_clear_auth(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "from-env-token")
        cf = _ConfigFields(relay_url=None, relay_auth=RelayAuth(token="from-file"))
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "tls://from-env:4222"
        assert result.relay_auth == RelayAuth(token="from-env-token")

    def test_no_url_env_var_leaves_auth_and_tls_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env-only auth override (no URL) leaves TLS and URL as file-resolved.

        The file must already resolve a relay URL (or the auth override
        would fall through to the local relay -- rejected by the fail-fast
        check this same override function performs).
        """
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "rotated-token")
        cf = _ConfigFields(
            relay_url="nats://committed:4222",
            relay_allow_env_override=True,
            relay_auth=RelayAuth(token="stale-token"),
            relay_tls_handshake_first=True,
        )
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "nats://committed:4222"
        assert result.relay_tls_handshake_first is True
        assert result.relay_auth == RelayAuth(token="rotated-token")

    def test_empty_string_env_vars_treated_as_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset repo *variable* in a workflow env: block expands to ''."""
        monkeypatch.setenv("BIFF_RELAY_URL", "")
        cf = _ConfigFields(relay_url=None, relay_auth=RelayAuth(token="unchanged"))
        result = _apply_env_relay_overrides(cf)
        assert result == cf

    def test_missing_nkeys_seed_file_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist.nk"
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", str(missing))
        with pytest.raises(SystemExit, match="BIFF_RELAY_NKEYS_SEED"):
            _apply_env_relay_overrides(_ConfigFields())

    def test_missing_user_credentials_file_fails_fast(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        missing = tmp_path / "does-not-exist.creds"
        monkeypatch.setenv("BIFF_RELAY_USER_CREDENTIALS", str(missing))
        with pytest.raises(SystemExit, match="BIFF_RELAY_USER_CREDENTIALS"):
            _apply_env_relay_overrides(_ConfigFields())

    def test_missing_token_value_never_needs_a_file(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BIFF_RELAY_TOKEN is a shared secret, not a path -- no fail-fast check."""
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "not-a-path")
        cf = _apply_env_relay_overrides(_ConfigFields())
        assert cf.relay_auth == RelayAuth(token="not-a-path")

    def test_gate_closed_when_url_resolved_and_no_opt_in(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A repo that already commits relay.url is not silently overridden."""
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://attacker:4222")
        cf = _ConfigFields(relay_url="tls://committed:4222", relay_committed=True)
        result = _apply_env_relay_overrides(cf)
        assert result == cf

    def test_gate_open_when_url_only_from_local_yaml(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A personal config.local.yaml relay entry does not close the gate.

        relay_committed tracks only the SHARED config.yaml -- the merged
        relay_url (which includes config.local.yaml) must never be used for
        the gate, or every developer with a personal relay override in
        config.local.yaml would have BIFF_RELAY_* silently blocked, even
        though the design's stated precedence is
        config.yaml < config.local.yaml < env vars.
        """
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        cf = _ConfigFields(
            relay_url="tls://from-local-yaml:4222", relay_committed=False
        )
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "tls://from-env:4222"

    def test_gate_open_when_opted_in(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        cf = _ConfigFields(
            relay_url="tls://committed:4222",
            relay_committed=True,
            relay_allow_env_override=True,
        )
        result = _apply_env_relay_overrides(cf)
        assert result.relay_url == "tls://from-env:4222"

    def test_gate_open_when_conflicting_but_url_not_set_bypasses_conflict_raise(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gate closure short-circuits before any env var is even read."""
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "x")
        monkeypatch.setenv("BIFF_RELAY_NKEYS_SEED", "/does-not-exist")
        cf = _ConfigFields(relay_url="tls://committed:4222", relay_committed=True)
        result = _apply_env_relay_overrides(cf)
        assert result == cf

    def test_url_and_token_together_reset_stale_tls_handshake_first(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Reset file-resolved TLS handshake mode when URL and auth are both set.

        The CI configuration in docs/relay-env-overrides.md sets both variables;
        neither URL-only nor combined overrides may inherit another relay's TLS mode.
        """
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "from-env-token")
        cf = _ConfigFields(
            relay_url=None,
            relay_auth=RelayAuth(token="from-file"),
            relay_tls_handshake_first=True,
        )
        result = _apply_env_relay_overrides(cf)
        assert result.relay_tls_handshake_first is False

    def test_url_and_token_with_explicit_tls_override_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The URL-change reset can still be overridden explicitly in the
        same call, e.g. re-enabling handshake-first for the new relay."""
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "from-env-token")
        monkeypatch.setenv("BIFF_RELAY_TLS_HANDSHAKE_FIRST", "true")
        cf = _ConfigFields(relay_url=None, relay_tls_handshake_first=False)
        result = _apply_env_relay_overrides(cf)
        assert result.relay_tls_handshake_first is True

    def test_auth_only_no_url_fails_fast_instead_of_local_relay(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """an auth-only override with no
        resolved relay URL must raise, not silently resolve to the demo
        relay -- this is the exact CI
        misconfiguration shape (missing/typo'd vars.BIFF_RELAY_URL) that
        would otherwise leave a real CI token unused with no error.
        """
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "real-ci-secret")
        with pytest.raises(SystemExit, match="BIFF_RELAY_TOKEN"):
            _apply_env_relay_overrides(_ConfigFields())

    def test_auth_only_no_url_never_resolves_through_the_full_chain(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the auth-only-no-URL misconfiguration must raise,
        not resolve to the local relay -- verified via the full resolution
        chain, not just the raw override function."""
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "real-ci-secret")
        with pytest.raises(SystemExit):
            _resolve_relay_fields(_ConfigFields())

    @pytest.mark.parametrize("value", ["0", "false", "FALSE", "no", "No"])
    def test_tls_handshake_first_explicit_false_is_distinguishable_from_unset(
        self, monkeypatch: pytest.MonkeyPatch, value: str
    ) -> None:
        """An explicit 'false' must reset True from the file layer.

        It must be logged as fired,
        proving it is a distinct code path from "unset" (which also
        resolves to False but never appends to the fired list).
        """
        monkeypatch.setenv("BIFF_RELAY_TLS_HANDSHAKE_FIRST", value)
        cf = _ConfigFields(relay_tls_handshake_first=True)
        assert _tls_env_override() is False
        result = _apply_env_relay_overrides(cf)
        assert result.relay_tls_handshake_first is False

    def test_tls_handshake_first_unset_is_none_not_false(self) -> None:
        """Unset must be distinguishable from explicit false at the helper
        level -- this is what makes bug 1b's fix possible."""
        assert _tls_env_override() is None

    def test_fired_vars_logged_by_name(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "s3cret-value")
        with caplog.at_level(logging.INFO, logger=_CONFIG_LOGGER):
            _apply_env_relay_overrides(_ConfigFields())
        messages = " ".join(caplog.messages)
        assert "BIFF_RELAY_TOKEN" in messages
        assert "s3cret-value" not in messages


class TestApplyEnvRelayOverridesIntegration:
    """Env vars flow through the full load_cli_config chain, not just the unit."""

    @pytest.fixture(autouse=True)
    def _clear_relay_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in TestApplyEnvRelayOverrides._ENV_VARS:
            monkeypatch.delenv(name, raising=False)

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_env_var_reaches_final_biff_config(
        self, _mock_gh: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """BIFF_RELAY_URL set in the environment reaches the resolved BiffConfig.

        Zero-config repo (no config.yaml) -- the gate is open because the
        file-resolved relay_url is None.
        """
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "env-token")
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_url == "tls://from-env:4222"
        assert resolved.config.relay_auth == RelayAuth(token="env-token")

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_env_var_blocked_by_committed_relay_without_opt_in(
        self, _mock_gh: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A committed relay.url wins over an ambient env var by default."""
        _setup_repo_with_yaml(tmp_path)  # commits relay.url: nats://localhost:4222
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://attacker:4222")
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_url == "nats://localhost:4222"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_env_var_wins_with_explicit_opt_in(
        self, _mock_gh: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: nats://localhost:4222\n  allow_env_override: true\n"
        )
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_url == "tls://from-env:4222"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_env_var_not_blocked_by_personal_local_yaml_relay(
        self, _mock_gh: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A relay committed only in config.local.yaml does not close the gate.

        Only a team-committed relay.url in shared config.yaml closes the gate;
        a developer's personal relay entry must not prevent env overrides.
        """
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text(
            "relay:\n  url: nats://personal-relay:4222\n"
        )
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_url == "tls://from-env:4222"

    @patch("biff.identity.get_github_identity", return_value=_KAI)
    def test_local_yaml_opt_in_cannot_authorize_env_override(
        self, _mock_gh: object, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """allow_env_override in config.local.yaml never opens a shared gate.

        The opt-in bit must come
        from the shared config.yaml alone. A gitignored config.local.yaml
        setting relay.allow_env_override: true must not authorize ambient
        env vars to override the team's committed relay -- that decision
        belongs to what the team commits, not to one developer's untracked
        file.
        """
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("relay:\n  url: nats://localhost:4222\n")
        (biff_dir / "config.local.yaml").write_text(
            "relay:\n  allow_env_override: true\n"
        )
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://attacker:4222")
        resolved = load_cli_config(start=tmp_path)
        assert resolved.config.relay_url == "nats://localhost:4222"
