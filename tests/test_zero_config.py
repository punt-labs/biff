"""Tests for zero-config biff: YAML config pipeline and owner derivation."""

from __future__ import annotations

from pathlib import Path
from typing import cast
from unittest.mock import patch

import pytest
import yaml

from biff._stdlib import get_repo_owner, is_enabled, yaml_config_dir
from biff.config import (
    DEMO_RELAY_URL,
    GitHubIdentity,
    _deep_merge,
    demo_creds_path,
    ensure_gitignore_yaml,
    load_config,
    load_yaml_config,
    load_yaml_local,
    merge_config,
    write_yaml_config,
    write_yaml_local_enabled,
)
from biff.models import RelayAuth

# ── get_repo_owner ──────────────────────────────────────────────────


class TestGetRepoOwner:
    @patch("biff._stdlib.get_repo_slug", return_value="punt-labs/biff")
    def test_extracts_owner(self, _mock: object, tmp_path: Path) -> None:
        assert get_repo_owner(tmp_path) == "punt-labs"

    @patch("biff._stdlib.get_repo_slug", return_value=None)
    def test_no_remote_returns_none(self, _mock: object, tmp_path: Path) -> None:
        assert get_repo_owner(tmp_path) is None

    @patch("biff._stdlib.get_repo_slug", return_value="jmf-pobox/biff")
    def test_fork_returns_fork_owner(self, _mock: object, tmp_path: Path) -> None:
        assert get_repo_owner(tmp_path) == "jmf-pobox"

    @patch("biff._stdlib.get_repo_slug", return_value="my.org/repo")
    def test_dots_sanitized(self, _mock: object, tmp_path: Path) -> None:
        """Dots in owner name are replaced with dashes for NATS safety."""
        assert get_repo_owner(tmp_path) == "my-org"

    @patch("biff._stdlib.get_repo_slug", return_value="UPPER/repo")
    def test_preserves_case(self, _mock: object, tmp_path: Path) -> None:
        assert get_repo_owner(tmp_path) == "UPPER"


# ── yaml_config_dir ────────────────────────────────────────────────


class TestYamlConfigDir:
    def test_returns_correct_path(self, tmp_path: Path) -> None:
        assert yaml_config_dir(tmp_path) == tmp_path / ".punt-labs" / "biff"


# ── is_enabled (new path) ──────────────────────────────────────────


class TestIsEnabledYaml:
    def test_new_path_enabled_true(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: true\n")
        assert is_enabled(tmp_path) is True

    def test_new_path_enabled_false(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: false\n")
        assert is_enabled(tmp_path) is False

    def test_none_repo_root(self) -> None:
        assert is_enabled(None) is False

    def test_no_config_files(self, tmp_path: Path) -> None:
        assert is_enabled(tmp_path) is False

    def test_yaml_with_extra_whitespace(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("  enabled:   true  \n")
        assert is_enabled(tmp_path) is True

    def test_yaml_boolean_capital_true(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: True\n")
        assert is_enabled(tmp_path) is True

    def test_yaml_boolean_yes(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: yes\n")
        assert is_enabled(tmp_path) is True

    def test_yaml_boolean_on(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: on\n")
        assert is_enabled(tmp_path) is True


# ── load_yaml_config / load_yaml_local ─────────────────────────────


class TestLoadYamlConfig:
    def test_reads_config(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "enabled: true\nrelay:\n  url: tls://example.com\n"
        )
        result = load_yaml_config(tmp_path)
        assert result["enabled"] is True
        assert isinstance(result["relay"], dict)
        assert result["relay"]["url"] == "tls://example.com"

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_yaml_config(tmp_path) == {}

    def test_malformed_yaml_exits(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("{{not yaml")
        with pytest.raises(SystemExit, match="Failed to parse"):
            load_yaml_config(tmp_path)

    def test_non_dict_yaml_returns_empty(self, tmp_path: Path) -> None:
        """A YAML file containing a scalar returns empty dict."""
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("just a string\n")
        assert load_yaml_config(tmp_path) == {}

    def test_empty_file_returns_empty(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("")
        assert load_yaml_config(tmp_path) == {}


class TestLoadYamlLocal:
    def test_reads_local(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("enabled: false\n")
        result = load_yaml_local(tmp_path)
        assert result["enabled"] is False

    def test_returns_empty_when_missing(self, tmp_path: Path) -> None:
        assert load_yaml_local(tmp_path) == {}

    def test_malformed_yaml_returns_empty(self, tmp_path: Path) -> None:
        """Local config is lenient -- malformed returns empty, no exit."""
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.local.yaml").write_text("{{not yaml")
        assert load_yaml_local(tmp_path) == {}


# ── merge_config / _deep_merge ─────────────────────────────────────


class TestMergeConfig:
    def test_local_overrides_shared(self) -> None:
        shared: dict[str, object] = {"enabled": True, "relay": {"url": "a"}}
        local: dict[str, object] = {"enabled": False}
        merged = merge_config(shared, local)
        assert merged["enabled"] is False
        assert isinstance(merged["relay"], dict)
        assert merged["relay"]["url"] == "a"

    def test_deep_merge_relay(self) -> None:
        shared: dict[str, object] = {
            "relay": {"url": "tls://demo", "auth": {"token": "abc"}}
        }
        local: dict[str, object] = {"relay": {"url": "tls://custom"}}
        merged = merge_config(shared, local)
        assert isinstance(merged["relay"], dict)
        assert merged["relay"]["url"] == "tls://custom"
        # Auth from shared should survive the merge
        assert isinstance(merged["relay"]["auth"], dict)
        assert merged["relay"]["auth"]["token"] == "abc"

    def test_empty_local(self) -> None:
        shared: dict[str, object] = {"enabled": True}
        merged = merge_config(shared, {})
        assert merged == shared

    def test_empty_shared(self) -> None:
        local: dict[str, object] = {"enabled": True}
        merged = merge_config({}, local)
        assert merged == local

    def test_local_adds_new_keys(self) -> None:
        shared: dict[str, object] = {"relay": {"url": "a"}}
        local: dict[str, object] = {"peers": {"orgs": ["punt-labs"]}}
        merged = merge_config(shared, local)
        assert "relay" in merged
        assert "peers" in merged

    def test_non_dict_override_replaces(self) -> None:
        """When local has a non-dict where shared has a dict, replace."""
        shared: dict[str, object] = {"relay": {"url": "a"}}
        local: dict[str, object] = {"relay": "disabled"}
        merged = merge_config(shared, local)
        assert merged["relay"] == "disabled"


class TestDeepMerge:
    def test_three_levels(self) -> None:
        base: dict[str, object] = {"a": {"b": {"c": 1, "d": 2}}}
        override: dict[str, object] = {"a": {"b": {"c": 99}}}
        result = _deep_merge(base, override)
        assert isinstance(result["a"], dict)
        inner: dict[str, object] = cast("dict[str, object]", result["a"])
        assert isinstance(inner["b"], dict)
        assert inner["b"]["c"] == 99
        assert inner["b"]["d"] == 2


# ── write_yaml_config / write_yaml_local_enabled ───────────────────


class TestWriteYamlConfig:
    def test_writes_shared_config(self, tmp_path: Path) -> None:
        data: dict[str, object] = {"relay": {"url": "tls://example.com"}}
        path = write_yaml_config(tmp_path, data)
        assert path == tmp_path / ".punt-labs" / "biff" / "config.yaml"
        assert path.exists()
        loaded = yaml.safe_load(path.read_text())
        assert loaded["relay"]["url"] == "tls://example.com"

    def test_writes_local_config(self, tmp_path: Path) -> None:
        data: dict[str, object] = {"enabled": True}
        path = write_yaml_config(tmp_path, data, local=True)
        assert path.name == "config.local.yaml"
        loaded = yaml.safe_load(path.read_text())
        assert loaded["enabled"] is True

    def test_creates_directory(self, tmp_path: Path) -> None:
        data: dict[str, object] = {"enabled": True}
        path = write_yaml_config(tmp_path, data)
        assert path.parent.exists()


class TestWriteYamlLocalEnabled:
    def test_writes_enabled_true(self, tmp_path: Path) -> None:
        path = write_yaml_local_enabled(tmp_path, enabled=True)
        loaded = yaml.safe_load(path.read_text())
        assert loaded["enabled"] is True

    def test_writes_enabled_false(self, tmp_path: Path) -> None:
        path = write_yaml_local_enabled(tmp_path, enabled=False)
        loaded = yaml.safe_load(path.read_text())
        assert loaded["enabled"] is False

    def test_roundtrip_with_is_enabled(self, tmp_path: Path) -> None:
        write_yaml_local_enabled(tmp_path, enabled=True)
        assert is_enabled(tmp_path) is True
        write_yaml_local_enabled(tmp_path, enabled=False)
        assert is_enabled(tmp_path) is False


# ── ensure_gitignore_yaml ──────────────────────────────────────────


class TestEnsureGitignoreYaml:
    def test_creates_gitignore(self, tmp_path: Path) -> None:
        ensure_gitignore_yaml(tmp_path)
        gi = tmp_path / ".punt-labs" / "biff" / ".gitignore"
        assert gi.exists()
        assert "config.local.yaml" in gi.read_text()

    def test_idempotent(self, tmp_path: Path) -> None:
        ensure_gitignore_yaml(tmp_path)
        ensure_gitignore_yaml(tmp_path)
        gi = tmp_path / ".punt-labs" / "biff" / ".gitignore"
        assert gi.read_text().count("config.local.yaml") == 1

    def test_preserves_existing_entries(self, tmp_path: Path) -> None:
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / ".gitignore").write_text("other-file\n")
        ensure_gitignore_yaml(tmp_path)
        content = (biff_dir / ".gitignore").read_text()
        assert "other-file" in content
        assert "config.local.yaml" in content


# ── load_config: zero-config mode ──────────────────────────────────

_KAI = GitHubIdentity(login="kai", display_name="Kai Chen")


class TestLoadConfigZeroConfig:
    @patch("biff.config.get_repo_owner", return_value="punt-labs")
    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_zero_config_derives_org(
        self, _gh: object, _slug: object, _owner: object, tmp_path: Path
    ) -> None:
        """No config files -> org derived from remote owner."""
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.orgs == ("punt-labs",)

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_zero_config_uses_demo_relay(
        self, _gh: object, _slug: object, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_url == DEMO_RELAY_URL
        assert resolved.config.relay_auth is not None
        assert resolved.config.relay_auth.user_credentials == str(demo_creds_path())

    @patch("biff.config.get_repo_owner", return_value=None)
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_zero_config_no_remote_no_orgs(
        self, _gh: object, _owner: object, tmp_path: Path
    ) -> None:
        """No remote -> empty orgs, but still demo relay."""
        (tmp_path / ".git").mkdir()
        resolved = load_config(start=tmp_path)
        assert resolved.config.orgs == ()
        assert resolved.config.relay_url == DEMO_RELAY_URL


class TestLoadConfigYaml:
    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_explicit_yaml_config(
        self, _gh: object, _slug: object, tmp_path: Path
    ) -> None:
        """config.yaml exists -> explicit mode, values honored."""
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: nats://custom:4222\npeers:\n  orgs:\n    - my-org\n"
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_url == "nats://custom:4222"
        assert resolved.config.orgs == ("my-org",)

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_yaml_local_overrides_shared(
        self, _gh: object, _slug: object, tmp_path: Path
    ) -> None:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("relay:\n  url: tls://shared\n")
        (biff_dir / "config.local.yaml").write_text(
            "relay:\n  url: tls://local-override\n"
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_url == "tls://local-override"

    @patch("biff.config.get_repo_owner", return_value="punt-labs")
    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_yaml_config_without_orgs_derives_from_remote(
        self, _gh: object, _slug: object, _owner: object, tmp_path: Path
    ) -> None:
        """config.yaml with only [relay] -> orgs derived from remote.

        Regression test for biff_relay writing config.yaml with just
        relay section — without this fallback, orgs would be empty
        and org-scoped discovery silently broken.
        """
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("relay:\n  url: tls://custom\n")
        resolved = load_config(start=tmp_path)
        assert resolved.config.orgs == ("punt-labs",)
        assert resolved.config.relay_url == "tls://custom"

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_yaml_with_auth(self, _gh: object, _slug: object, tmp_path: Path) -> None:
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text(
            "relay:\n  url: tls://secure\n  auth:\n    credentials: /path/to.creds\n"
        )
        resolved = load_config(start=tmp_path)
        assert resolved.config.relay_url == "tls://secure"
        assert resolved.config.relay_auth == RelayAuth(
            user_credentials="/path/to.creds"
        )

    @patch("biff.config.get_repo_slug", return_value="punt-labs/biff")
    @patch("biff.config.get_github_identity", return_value=_KAI)
    def test_explicit_config_without_relay_gets_demo(
        self, _gh: object, _slug: object, tmp_path: Path
    ) -> None:
        """Explicit config that omits relay still gets demo relay as fallback."""
        (tmp_path / ".git").mkdir()
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / "config.yaml").write_text("peers:\n  orgs:\n    - punt-labs\n")
        resolved = load_config(start=tmp_path)
        # Demo relay is always the fallback
        assert resolved.config.relay_url == DEMO_RELAY_URL
