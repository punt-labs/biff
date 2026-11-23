"""The local relay is the default; demo and NATS relays are chosen explicitly."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from biff._stdlib import biff_data_dir
from biff.config import (
    DEMO_CREDS_PATH,
    DEMO_RELAY_URL,
    ResolvedConfig,
    load_cli_config,
    resolve_relay_config,
)
from biff.identity import GitHubIdentity
from biff.models import RelayAuth

_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")


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
        assert resolve_relay_config(tmp_path) == (None, None, False)

    def test_config_without_relay_section_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "peers:\n  orgs:\n    - punt-labs\n")
        assert resolve_relay_config(repo) == (None, None, False)

    def test_local_yaml_without_relay_section_selects_local(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, local="poll_interval: 5\n")
        assert resolve_relay_config(repo) == (None, None, False)

    def test_empty_url_still_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, 'relay:\n  url: ""\n')
        assert resolve_relay_config(repo) == (None, None, False)


class TestRelayType:
    def test_type_local_selects_local(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: local\n")
        assert resolve_relay_config(repo) == (None, None, False)

    def test_type_demo_selects_demo_relay_with_bundled_credentials(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n")
        url, auth, tls = resolve_relay_config(repo)
        assert url == DEMO_RELAY_URL
        assert auth == RelayAuth(user_credentials=str(DEMO_CREDS_PATH))
        assert tls is False

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
        url, auth, _ = resolve_relay_config(repo)
        assert url == DEMO_RELAY_URL
        assert auth == RelayAuth(user_credentials=str(DEMO_CREDS_PATH))

    def test_local_yaml_type_overrides_shared_url(self, tmp_path: Path) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  url: nats://shared:4222\n",
            local="relay:\n  type: local\n",
        )
        assert resolve_relay_config(repo) == (None, None, False)

    def test_type_local_drops_auth_merged_from_shared_config(
        self, tmp_path: Path
    ) -> None:
        repo = _repo(
            tmp_path,
            "relay:\n  type: nats\n  url: nats://shared:4222\n"
            "  auth:\n    token: s3cret\n",
            local="relay:\n  type: local\n",
        )
        assert resolve_relay_config(repo) == (None, None, False)

    def test_type_local_ignores_a_url_in_the_same_file(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: local\n  url: nats://x\n")
        assert resolve_relay_config(repo) == (None, None, False)

    def test_type_demo_ignores_a_url_in_the_same_file(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path, "relay:\n  type: demo\n  url: nats://x\n")
        assert resolve_relay_config(repo)[0] == DEMO_RELAY_URL

    @pytest.mark.parametrize(
        ("config", "message"),
        [
            ("relay:\n  type: nats\n", "type: nats requires"),
            ("relay:\n  type: docker\n", "Unknown relay type 'docker'"),
        ],
    )
    def test_inconsistent_relay_section_exits(
        self, tmp_path: Path, config: str, message: str
    ) -> None:
        repo = _repo(tmp_path, config)
        with pytest.raises(SystemExit, match=message):
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


def _load(repo: Path) -> ResolvedConfig:
    with patch("biff.identity.get_github_identity", return_value=_KAI):
        return load_cli_config(start=repo)


class TestLocalRelayLocation:
    def test_directory_is_per_user_and_durable(
        self, tmp_path: Path, _isolated_home: Path
    ) -> None:
        repo = _repo(tmp_path / "work")
        with patch("biff.config.get_repo_slug", return_value="punt-labs/biff"):
            resolved = _load(repo)
        assert resolved.data_dir == biff_data_dir() / "relay" / "punt-labs__biff"
        assert resolved.data_dir.is_relative_to(_isolated_home)

    def test_repos_with_the_same_name_get_different_directories(
        self, tmp_path: Path
    ) -> None:
        one = _repo(tmp_path / "a" / "app")
        two = _repo(tmp_path / "b" / "app")
        assert _load(one).data_dir != _load(two).data_dir

    def test_no_remote_key_is_stable_across_loads(self, tmp_path: Path) -> None:
        repo = _repo(tmp_path / "app")
        assert _load(repo).data_dir == _load(repo).data_dir

    def test_two_clones_of_one_remote_share_a_directory(self, tmp_path: Path) -> None:
        one = _repo(tmp_path / "a" / "biff")
        two = _repo(tmp_path / "b" / "biff")
        with patch("biff.config.get_repo_slug", return_value="punt-labs/biff"):
            assert _load(one).data_dir == _load(two).data_dir
