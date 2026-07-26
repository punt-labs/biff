"""Tests for the shared committed-enablement artifacts (``RepoEnablement``).

``RepoEnablement`` is the single definition of what ``enable``/``disable`` do,
so both front-ends (the CLI verbs and the MCP ``biff`` tool) produce an
identical committed result (DES-052, biff-j5u).  These tests pin the committed
set to exactly ``{marker, CI workflow}`` and prove ``.git/hooks/`` is never
touched.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.enablement import RepoEnablement

if TYPE_CHECKING:
    from pathlib import Path


def _make_repo(root: Path) -> Path:
    """Create a bare git-repo skeleton with a ``.git/hooks`` directory."""
    (root / ".git" / "hooks").mkdir(parents=True)
    return root


class TestEnable:
    def test_writes_marker(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".punt-labs" / "biff" / "enabled").is_file()

    def test_writes_ci_workflow(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".github" / "workflows" / "biff-notify.yml").is_file()

    def test_never_touches_git_hooks(self, tmp_path: Path) -> None:
        """Enable writes only committed artifacts — hooks are per-clone (install)."""
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        deployed = list((tmp_path / ".git" / "hooks").iterdir())
        assert deployed == []

    def test_idempotent(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".punt-labs" / "biff" / "enabled").is_file()
        assert (tmp_path / ".github" / "workflows" / "biff-notify.yml").is_file()

    def test_reports_ci_workflow_change(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        first = RepoEnablement(tmp_path).enable()
        assert first.ci_workflow_changed is True
        second = RepoEnablement(tmp_path).enable()
        assert second.ci_workflow_changed is False  # already up to date


class TestDisable:
    def test_removes_marker(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        RepoEnablement(tmp_path).disable()
        assert not (tmp_path / ".punt-labs" / "biff" / "enabled").exists()

    def test_removes_ci_workflow(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        RepoEnablement(tmp_path).disable()
        assert not (tmp_path / ".github" / "workflows" / "biff-notify.yml").exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).disable()
        RepoEnablement(tmp_path).disable()
        assert not (tmp_path / ".punt-labs" / "biff" / "enabled").exists()
