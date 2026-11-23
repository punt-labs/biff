"""The local relay is the default; demo and NATS relays are chosen explicitly."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from biff._stdlib import biff_data_dir
from biff.config import (
    ResolvedConfig,
    load_cli_config,
    resolve_relay_config,
)
from biff.identity import GitHubIdentity
from biff.models import RelayAuth
from biff.relay_config import (
    DEMO_CREDS_PATH,
    DEMO_RELAY_URL,
    local_relay_location,
    remote_relay_key,
)

_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")
_DEMO = RelayAuth(user_credentials=str(DEMO_CREDS_PATH))
_LOCAL = (None, None, False)


def _repo(tmp_path: Path, config: str | None = None, local: str | None = None) -> Path:
    """Create a git repo, optionally with shared and local relay config."""
    (tmp_path / ".git").mkdir(parents=True)
    biff_dir = tmp_path / ".punt-labs" / "biff"
    biff_dir.mkdir(parents=True)
    if config is not None:
        (biff_dir / "config.yaml").write_text(config)
    if local is not None:
        (biff_dir / "config.local.yaml").write_text(local)
    return tmp_path


@pytest.fixture(autouse=True)
def _isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    """Point HOME at an empty directory and clear relay environment overrides."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    for name in (
        "BIFF_RELAY_URL",
        "BIFF_RELAY_TOKEN",
        "BIFF_RELAY_NKEYS_SEED",
        "BIFF_RELAY_CREDS",
        "BIFF_RELAY_TLS_HANDSHAKE_FIRST",
    ):
        monkeypatch.delenv(name, raising=False)
    return home


class TestDefaultIsLocal:
    def test_no_config_files_selects_local(self, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        assert resolve_relay_config(tmp_path) == _LOCAL

    def test_config_without_relay_section_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "peers:\n  orgs:\n    - punt-labs\n")
        assert resolve_relay_config(repo) == _LOCAL

    def test_local_yaml_without_relay_section_selects_local(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, local="poll_interval: 5\n")
        assert resolve_relay_config(repo) == _LOCAL

    def test_empty_url_still_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, 'relay:\n  url: ""\n')
        assert resolve_relay_config(repo) == _LOCAL

    def test_empty_relay_key_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n")
        assert resolve_relay_config(repo) == _LOCAL


class TestRelayType:
    def test_type_local_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: local\n")
        assert resolve_relay_config(repo) == _LOCAL

    def test_type_demo_selects_demo_relay_with_bundled_credentials(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n")
        assert resolve_relay_config(repo) == (DEMO_RELAY_URL, _DEMO, False)

    def test_type_nats_uses_the_url(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: nats\n  url: nats://host:4222\n")
        assert resolve_relay_config(repo) == ("nats://host:4222", None, False)

    def test_url_without_type_is_nats_and_unchanged(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  url: tls://relay.example\n  auth:\n    token: s3cret\n"
            "  tls_handshake_first: true\n",
        )
        assert resolve_relay_config(repo) == (
            "tls://relay.example",
            RelayAuth(token="s3cret"),
            True,
        )

    def test_demo_url_without_type_still_gets_bundled_credentials(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, f"relay:\n  url: {DEMO_RELAY_URL}\n")
        assert resolve_relay_config(repo) == (DEMO_RELAY_URL, _DEMO, False)

    def test_personal_credentials_apply_to_the_committed_relay(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  url: tls://relay.example\n",
            local="relay:\n  auth:\n    token: mine\n",
        )
        assert resolve_relay_config(repo) == (
            "tls://relay.example",
            RelayAuth(token="mine"),
            False,
        )


class TestPerFilePrecedence:
    """The highest file that sets type or url decides the relay."""

    @pytest.mark.parametrize(
        ("committed", "personal", "expected"),
        [
            ("type: demo", 'url: ""', None),
            ("type: demo", "url: nats://mine", "nats://mine"),
            ("type: local", "url: nats://mine", "nats://mine"),
            ("type: nats\n  url: nats://x", 'url: ""', None),
            ("url: nats://x", "type: local", None),
            ("type: local", "type: demo", DEMO_RELAY_URL),
        ],
    )
    def test_personal_file_decides(
        self, tmp_path: Path, committed: str, personal: str, expected: str | None
    ) -> None:
        repo = _repo(
            tmp_path,
            f"relay:\n  {committed}\n",
            local=f"relay:\n  {personal}\n",
        )
        assert resolve_relay_config(repo)[0] == expected

    def test_committed_credentials_do_not_leak_onto_a_personal_relay(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  url: nats://shared:4222\n  auth:\n    token: team\n",
            local="relay:\n  url: nats://mine:4222\n",
        )
        assert resolve_relay_config(repo) == ("nats://mine:4222", None, False)


class TestRelayConfigErrors:
    @pytest.mark.parametrize(
        ("config", "message"),
        [
            ("relay:\n  type: nats\n", "type: nats requires"),
            ("relay:\n  type: docker\n", "Unknown relay type 'docker'"),
            ("relay: nats://x\n", "'relay' must be a mapping"),
            ("relay:\n  - url: x\n", "'relay' must be a mapping"),
            ("relay:\n  url: [1]\n", "relay.url must be a string"),
            ("relay:\n  url: 5\n", "relay.url must be a string"),
            ("relay:\n  urll: nats://x\n", "unknown key.*urll"),
            ("relays:\n  url: nats://x\n", "unknown key 'relays'"),
            (
                "relay:\n  url: nats://x\n  tls_handshake_first: yes-please\n",
                "tls_handshake_first must be true or false",
            ),
            (
                "relay:\n  url: nats://x\n  tls_handshake_first: 1\n",
                "tls_handshake_first must be true or false",
            ),
            (
                "relay:\n  url: nats://x\n  allow_env_override: 'true'\n",
                "allow_env_override must be true or false",
            ),
            ("relay:\n  type: demo\n  url: nats://x\n", "type: demo.*url"),
            ("relay:\n  type: demo\n  token: mine\n", "type: demo.*auth"),
            ("relay:\n  type: demo\n  nkeys_seed: /k\n", "type: demo.*auth"),
            ("relay:\n  type: demo\n  user_credentials: /c\n", "type: demo.*auth"),
            ("relay:\n  type: demo\n  auth:\n    token: mine\n", "type: demo.*auth"),
            (
                "relay:\n  type: demo\n  tls_handshake_first: true\n",
                "type: demo.*tls_handshake_first",
            ),
            ("relay:\n  type: local\n  url: nats://x\n", "type: local.*url"),
            ("relay:\n  type: local\n  token: t\n", "type: local.*auth"),
        ],
    )
    def test_named_error(self, tmp_path: Path, config: str, message: str) -> None:
        repo = _repo(tmp_path, config)
        with pytest.raises(SystemExit, match=message):
            resolve_relay_config(repo)

    def test_errors_name_the_offending_file(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, local="relay: nope\n")
        with pytest.raises(SystemExit, match=r"config\.local\.yaml"):
            resolve_relay_config(repo)

    def test_personal_credentials_never_reach_a_committed_demo_relay(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  type: demo\n",
            local="relay:\n  auth:\n    token: mine\n",
        )
        with pytest.raises(SystemExit, match="public demo relay"):
            resolve_relay_config(repo)

    def test_credentials_with_no_relay_chosen_is_an_error(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, local="relay:\n  token: mine\n")
        with pytest.raises(SystemExit, match="no relay type or url"):
            resolve_relay_config(repo)


class TestEnvOverrides:
    def test_env_url_applies_to_repo_with_no_relay_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        assert resolve_relay_config(tmp_path)[0] == "tls://from-env:4222"

    def test_committed_type_local_blocks_env_override(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: local\n")
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://from-env:4222")
        assert resolve_relay_config(repo)[0] is None

    def test_auth_env_without_url_names_the_missing_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / ".git").mkdir()
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "secret")
        with pytest.raises(SystemExit, match="ignored by the local relay"):
            resolve_relay_config(tmp_path)


def _git(cwd: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.com",
    }
    subprocess.run(  # noqa: S603
        ["git", "-C", str(cwd), *args],  # noqa: S607
        check=True,
        capture_output=True,
        env=env,
    )


def _clone(path: Path, *remotes: tuple[str, str]) -> Path:
    """Create a real git repo with (name, url) remotes."""
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "commit", "-q", "--allow-empty", "-m", "x")
    for name, url in remotes:
        _git(path, "remote", "add", name, url)
    return path


class TestRemoteRelayKey:
    """One remote identity, one key; different identities, different keys."""

    @pytest.mark.parametrize(
        ("one", "two"),
        [
            ("git@github.com:punt-labs/biff.git", "https://github.com/punt-labs/biff"),
            ("https://github.com/punt-labs/biff/", "https://github.com/punt-labs/biff"),
            ("https://github.com/Punt-Labs/Biff", "https://github.com/punt-labs/biff"),
            (
                "ssh://git@github.com/punt-labs/biff.git",
                "git@github.com:punt-labs/biff",
            ),
            (
                "ssh://git@github.com:22/punt-labs/biff",
                "https://github.com/punt-labs/biff.git",
            ),
            (
                "https://GitHub.com/punt-labs/biff.git/",
                "git@github.com:punt-labs/biff.git",
            ),
            (
                "https://git-manager.devin.ai/proxy/github.com/punt-labs/biff",
                "git@github.com:punt-labs/biff.git",
            ),
            ("https://gitlab.com/g/sub/repo", "git@gitlab.com:g/sub/repo.git"),
            ("https://gitlab.com/g/sub/repo/", "https://GitLab.com/G/Sub/Repo.git"),
        ],
    )
    def test_same_identity_shares_a_key(self, one: str, two: str) -> None:
        assert remote_relay_key(one)[0] == remote_relay_key(two)[0]

    @pytest.mark.parametrize(
        ("one", "two"),
        [
            ("https://github.com/o/my.repo", "https://github.com/o/my-repo"),
            ("https://github.com/o/r", "https://gitlab.com/o/r"),
            ("https://github.com/a/r", "https://github.com/b/r"),
            ("https://gitlab.com/g/one/repo", "https://gitlab.com/g/two/repo"),
            ("https://github.com/o/r", "https://gitlab.com/g/o/r"),
        ],
    )
    def test_different_identities_never_share_a_key(self, one: str, two: str) -> None:
        assert remote_relay_key(one)[0] != remote_relay_key(two)[0]

    def test_owner_repo_key_is_a_readable_slug_plus_hash(self) -> None:
        key, reason = remote_relay_key("git@github.com:Punt-Labs/Biff.git")
        assert key.startswith("punt-labs__biff-")
        assert len(key) == len("punt-labs__biff-") + 8
        assert "slug" in reason

    def test_subgroup_remote_is_keyed_by_its_url_hash(self) -> None:
        key, reason = remote_relay_key("https://gitlab.com/g/sub/repo")
        assert key.startswith("remote-")
        assert "URL hash" in reason


class TestLocalRelayLocation:
    def test_remote_repo_lives_in_the_per_user_relay_root(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        repo = _clone(tmp_path / "work", ("origin", "git@github.com:o/r.git"))
        location = local_relay_location(repo)
        assert location.path.parent == biff_data_dir() / "relay"
        assert location.path.is_relative_to(_isolated_home)
        assert "slug" in location.reason

    def test_ssh_clone_and_https_clone_with_trailing_slash_share_a_relay(
        self, tmp_path: Path
    ) -> None:
        one = _clone(tmp_path / "a", ("origin", "git@github.com:Punt-Labs/biff.git"))
        two = _clone(tmp_path / "b", ("origin", "https://github.com/punt-labs/biff/"))
        assert local_relay_location(one).path == local_relay_location(two).path

    def test_only_remote_is_used_when_it_is_not_called_origin(
        self, tmp_path: Path
    ) -> None:
        one = _clone(tmp_path / "a", ("upstream", "git@github.com:o/r.git"))
        two = _clone(tmp_path / "b", ("origin", "git@github.com:o/r.git"))
        assert local_relay_location(one).path == local_relay_location(two).path

    def test_origin_wins_among_several_remotes(self, tmp_path: Path) -> None:
        one = _clone(
            tmp_path / "a",
            ("fork", "git@github.com:me/r.git"),
            ("origin", "git@github.com:o/r.git"),
        )
        two = _clone(tmp_path / "b", ("origin", "git@github.com:o/r.git"))
        assert local_relay_location(one).path == local_relay_location(two).path

    def test_several_remotes_without_origin_count_as_no_remote(
        self, tmp_path: Path
    ) -> None:
        repo = _clone(
            tmp_path / "a",
            ("one", "git@github.com:o/r.git"),
            ("two", "git@github.com:o/s.git"),
        )
        assert "no remote" in local_relay_location(repo).reason

    def test_repos_with_the_same_name_and_no_remote_stay_apart(
        self, tmp_path: Path
    ) -> None:
        one = _clone(tmp_path / "a" / "app")
        two = _clone(tmp_path / "b" / "app")
        assert local_relay_location(one).path != local_relay_location(two).path

    def test_no_remote_key_is_stable(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app")
        assert local_relay_location(repo) == local_relay_location(repo)

    def test_linked_worktree_of_a_no_remote_repo_shares_its_main_checkout(
        self, tmp_path: Path
    ) -> None:
        main = _clone(tmp_path / "app")
        worktree = tmp_path / "app-wt"
        _git(main, "worktree", "add", "-q", str(worktree), "-b", "wt")
        assert local_relay_location(worktree).path == local_relay_location(main).path
        assert (worktree / ".git").is_file()

    def test_linked_worktree_of_a_remote_repo_shares_its_main_checkout(
        self, tmp_path: Path
    ) -> None:
        main = _clone(tmp_path / "app", ("origin", "git@github.com:o/r.git"))
        worktree = tmp_path / "app-wt"
        _git(main, "worktree", "add", "-q", str(worktree), "-b", "wt")
        assert local_relay_location(worktree).path == local_relay_location(main).path

    def test_loaded_config_uses_the_same_directory(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app", ("origin", "git@github.com:o/r.git"))
        with patch("biff.identity.get_github_identity", return_value=_KAI):
            resolved: ResolvedConfig = load_cli_config(start=repo)
        assert resolved.data_dir == local_relay_location(repo).path
        assert resolved.config.repo_name == "o__r"
