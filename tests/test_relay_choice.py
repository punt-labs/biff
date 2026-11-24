"""The local relay is the default; demo and NATS relays are chosen explicitly."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

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
    RelayLocation,
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


class TestLocalRelayLocation:
    _ZONE = Path(".punt-labs", "local", "biff", "relay")

    def test_relay_lives_in_the_checkouts_private_zone(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        repo = _clone(tmp_path / "work", ("origin", "git@github.com:o/r.git"))
        location = RelayLocation.of(repo)
        assert location.path == repo.resolve() / self._ZONE
        assert not location.path.is_relative_to(_isolated_home)
        assert "main checkout" in location.reason

    def test_checkouts_with_the_same_name_stay_apart(self, tmp_path: Path) -> None:
        one = _clone(tmp_path / "a" / "app", ("origin", "git@github.com:o/r.git"))
        two = _clone(tmp_path / "b" / "app", ("origin", "git@github.com:o/r.git"))
        assert RelayLocation.of(one).path != RelayLocation.of(two).path

    def test_location_is_stable(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app")
        assert RelayLocation.of(repo) == RelayLocation.of(repo)

    def test_linked_worktree_uses_its_main_checkouts_relay(
        self, tmp_path: Path
    ) -> None:
        main = _clone(tmp_path / "app")
        worktree = tmp_path / "app-wt"
        _git(main, "worktree", "add", "-q", str(worktree), "-b", "wt")
        assert RelayLocation.of(worktree).path == main.resolve() / self._ZONE
        assert (worktree / ".git").is_file()

    def test_sibling_submodules_keep_their_own_relays(self, tmp_path: Path) -> None:
        lib = _clone(tmp_path / "lib")
        sup = _clone(tmp_path / "super")
        for name in ("one", "two"):
            _git(
                sup,
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(lib),
                name,
            )
        one = RelayLocation.of(sup / "one").path
        two = RelayLocation.of(sup / "two").path
        assert one == (sup / "one").resolve() / self._ZONE
        assert two == (sup / "two").resolve() / self._ZONE

    def test_subdirectory_resolves_to_the_checkout(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _clone(tmp_path / "app")
        sub = repo / "src"
        sub.mkdir()
        monkeypatch.chdir(sub)
        assert RelayLocation.of(sub).path == repo.resolve() / self._ZONE

    def test_without_git_the_given_root_owns_the_relay(self, tmp_path: Path) -> None:
        with patch("biff.relay_config.get_repo_common_root", return_value=""):
            location = RelayLocation.of(tmp_path)
        assert location.path == tmp_path.resolve() / self._ZONE

    def test_relay_directory_is_private_and_ignored_by_git(
        self, tmp_path: Path
    ) -> None:
        repo = _clone(tmp_path / "app")
        RelayLocation.ensure(RelayLocation.of(repo).path)
        owned = repo / ".punt-labs" / "local" / "biff"
        for path in (owned, owned / "relay"):
            assert path.stat().st_mode & 0o777 == 0o700, path
        (owned / "relay" / "inbox-kai.jsonl").write_text("{}\n")
        status = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "status", "--porcelain"],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        assert status.stdout == ""

    def test_a_stale_gitignore_in_biffs_zone_is_replaced(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app")
        owned = repo / ".punt-labs" / "local" / "biff"
        owned.mkdir(parents=True)
        (owned / ".gitignore").write_text("other\n")
        RelayLocation.ensure(RelayLocation.of(repo).path)
        assert (owned / ".gitignore").read_text() == "*\n"

    def test_a_symlinked_zone_is_refused(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (repo / ".punt-labs").symlink_to(elsewhere)
        with pytest.raises(OSError, match="symlink"):
            RelayLocation.ensure(RelayLocation.of(repo).path)
        assert list(elsewhere.iterdir()) == []

    def test_a_symlinked_gitignore_is_replaced_not_followed(
        self, tmp_path: Path
    ) -> None:
        repo = _clone(tmp_path / "app")
        owned = repo / ".punt-labs" / "local" / "biff"
        owned.mkdir(parents=True)
        outside = tmp_path / "outside.txt"
        outside.write_text("keep\n")
        (owned / ".gitignore").symlink_to(outside)
        RelayLocation.ensure(RelayLocation.of(repo).path)
        assert outside.read_text() == "keep\n"
        assert not (owned / ".gitignore").is_symlink()
        assert (owned / ".gitignore").read_text() == "*\n"

    def test_data_dir_override_is_honored(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app", ("origin", "git@github.com:o/r.git"))
        override = tmp_path / "operator-relay"
        with patch("biff.identity.get_github_identity", return_value=_KAI):
            resolved = load_cli_config(start=repo, data_dir_override=override)
        assert resolved.data_dir == override

    def test_loaded_config_uses_the_same_directory(self, tmp_path: Path) -> None:
        repo = _clone(tmp_path / "app", ("origin", "git@github.com:o/r.git"))
        with patch("biff.identity.get_github_identity", return_value=_KAI):
            resolved: ResolvedConfig = load_cli_config(start=repo)
        assert resolved.data_dir == RelayLocation.of(repo).path
        assert resolved.config.repo_name == "o__r"


class TestDemoRelayNeverGetsEnvCredentials:
    def test_env_token_with_a_committed_demo_relay_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n  allow_env_override: true\n")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "private")
        with pytest.raises(SystemExit, match="public demo relay"):
            resolve_relay_config(repo)

    def test_env_url_and_token_replace_the_demo_relay(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n  allow_env_override: true\n")
        monkeypatch.setenv("BIFF_RELAY_URL", "tls://mine:4222")
        monkeypatch.setenv("BIFF_RELAY_TOKEN", "private")
        assert resolve_relay_config(repo) == (
            "tls://mine:4222",
            RelayAuth(token="private"),
            False,
        )


class TestPrivateAccountOnTheDemoEndpoint:
    """The demo URL is shared; only the bundled demo selection refuses credentials."""

    def test_an_explicit_url_takes_your_own_credentials(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        creds = tmp_path / "mine.creds"
        creds.write_text("creds")
        repo = _repo(
            tmp_path,
            f"relay:\n  url: {DEMO_RELAY_URL}\n  allow_env_override: true\n",
        )
        monkeypatch.setenv("BIFF_RELAY_USER_CREDENTIALS", str(creds))
        url, auth, _ = resolve_relay_config(repo)
        assert url == DEMO_RELAY_URL
        assert auth == RelayAuth(user_credentials=str(creds))

    def test_an_env_url_with_credentials_replaces_the_bundled_demo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        creds = tmp_path / "mine.creds"
        creds.write_text("creds")
        repo = _repo(tmp_path, "relay:\n  type: demo\n  allow_env_override: true\n")
        monkeypatch.setenv("BIFF_RELAY_URL", DEMO_RELAY_URL)
        monkeypatch.setenv("BIFF_RELAY_USER_CREDENTIALS", str(creds))
        assert resolve_relay_config(repo)[1] == RelayAuth(user_credentials=str(creds))
