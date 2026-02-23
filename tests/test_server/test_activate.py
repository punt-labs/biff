"""Tests for lazy activation (auto-enable on first tool use)."""

from __future__ import annotations

import tomllib
from pathlib import Path

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
        # Pre-create .biff so activation only writes .biff.local
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
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

    def test_writes_biff_local_enabled(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        biff_local = tmp_path / ".biff.local"
        assert biff_local.exists()
        parsed = tomllib.loads(biff_local.read_text())
        assert parsed["enabled"] is True

    def test_creates_biff_file_if_missing(self, tmp_path: Path) -> None:
        config = BiffConfig(
            user="kai",
            repo_name="test",
            team=("kai", "eric"),
            relay_url="tls://connect.ngs.global",
        )
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        biff_file = tmp_path / ".biff"
        assert biff_file.exists()
        parsed = tomllib.loads(biff_file.read_text())
        assert "kai" in parsed["team"]["members"]
        assert "eric" in parsed["team"]["members"]
        assert parsed["relay"]["url"] == "tls://connect.ngs.global"

    def test_does_not_overwrite_existing_biff(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        original = '[team]\nmembers = ["priya"]\n'
        (tmp_path / ".biff").write_text(original)
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        assert (tmp_path / ".biff").read_text() == original

    def test_ensures_gitignore_entry(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        gitignore = tmp_path / ".gitignore"
        assert gitignore.exists()
        assert ".biff.local" in gitignore.read_text()

    def test_gitignore_idempotent(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        (tmp_path / ".biff").write_text('[team]\nmembers = ["kai"]\n')
        (tmp_path / ".gitignore").write_text("node_modules/\n.biff.local\n")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        content = (tmp_path / ".gitignore").read_text()
        assert content.count(".biff.local") == 1

    def test_uses_demo_relay_when_no_relay_url(self, tmp_path: Path) -> None:
        config = BiffConfig(user="kai", repo_name="test")
        state = create_state(config, tmp_path, dormant=True, repo_root=tmp_path)
        lazy_activate(state)

        biff_file = tmp_path / ".biff"
        assert biff_file.exists()
        parsed = tomllib.loads(biff_file.read_text())
        assert parsed["relay"]["url"] == "tls://connect.ngs.global"
