"""Tests for lazy activation (auto-enable on first tool use)."""

from __future__ import annotations

from pathlib import Path

import yaml

from biff.models import BiffConfig
from biff.server.state import create_state
from biff.server.tools._activate import lazy_activate


class TestLazyActivate:
    """Unit tests for the lazy_activate() helper."""

    def test_returns_none_when_not_dormant(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, repo_root=tmp_path)
        assert lazy_activate(state) is None

    def test_returns_message_when_dormant(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        result = lazy_activate(state)
        assert result is not None
        assert "Restart" in result

    def test_returns_error_when_no_repo_root(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=None)
        result = lazy_activate(state)
        assert result is not None
        assert "not in a git repository" in result

    def test_writes_config_local_yaml(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        local_yaml = tmp_path / ".punt-labs" / "biff" / "config.local.yaml"
        assert local_yaml.exists()
        parsed = yaml.safe_load(local_yaml.read_text())
        assert parsed["enabled"] is True

    def test_does_not_create_config_yaml(self, tmp_path: Path) -> None:
        """Zero-config: only config.local.yaml is created, not config.yaml."""
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        config_yaml = tmp_path / ".punt-labs" / "biff" / "config.yaml"
        assert not config_yaml.exists()

    def test_ensures_gitignore_entry(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        gitignore = tmp_path / ".punt-labs" / "biff" / ".gitignore"
        assert gitignore.exists()
        assert "config.local.yaml" in gitignore.read_text()

    def test_gitignore_idempotent(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        biff_dir = tmp_path / ".punt-labs" / "biff"
        biff_dir.mkdir(parents=True)
        (biff_dir / ".gitignore").write_text("config.local.yaml\n")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        content = (biff_dir / ".gitignore").read_text()
        assert content.count("config.local.yaml") == 1
