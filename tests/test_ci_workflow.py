"""Tests for CI workflow deployment.

Unit tests for deploy/remove/check operations on
``.github/workflows/biff-notify.yml``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from biff.ci_workflow import (
    _WORKFLOW_NAME,
    _template_content,
    check_ci_workflow,
    deploy_ci_workflow,
    remove_ci_workflow,
)


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal directory structure (no .git needed for CI workflow)."""
    return tmp_path


# ── deploy_ci_workflow ─────────────────────────────────────────────


class TestDeployCiWorkflow:
    """CI workflow deployment — create, update, idempotent."""

    def test_creates_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert deploy_ci_workflow(repo) is True

        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        assert target.exists()
        assert target.read_text() == _template_content()

    def test_creates_directories(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert (repo / ".github" / "workflows").is_dir()

    def test_idempotent(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert deploy_ci_workflow(repo) is True
        assert deploy_ci_workflow(repo) is False  # No change

    def test_updates_stale_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        target.parent.mkdir(parents=True)
        target.write_text("old content")

        assert deploy_ci_workflow(repo) is True
        assert target.read_text() == _template_content()

    def test_unusable_root_raises(self) -> None:
        """A non-directory root is a real failure, not a silent ``False`` no-op."""
        with pytest.raises(ValueError, match="no usable repo root"):
            deploy_ci_workflow(Path("/nonexistent"))

    def test_replaces_symlinked_target(self, tmp_path: Path) -> None:
        """A symlink at the workflow path is replaced, never followed.

        Mirrors ``write_enabled_marker``'s symlink guard: an untrusted checkout
        could commit ``biff-notify.yml`` as a symlink pointing outside the repo;
        enabling must overwrite the link with a real file, not clobber the
        link's target.  Both surfaces (`biff enable`, `/biff enable`) reach this
        path, so the guard closes a symlink-follow write.
        """
        repo = _make_repo(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("do not touch")

        workflows = repo / ".github" / "workflows"
        workflows.mkdir(parents=True)
        link = workflows / _WORKFLOW_NAME
        link.symlink_to(outside)

        assert deploy_ci_workflow(repo) is True
        assert not link.is_symlink()
        assert link.read_text() == _template_content()
        # The symlink target is untouched.
        assert outside.read_text() == "do not touch"

    def test_symlinked_github_parent_cannot_escape(self, tmp_path: Path) -> None:
        """A committed symlinked ``.github`` must not redirect the write out of repo.

        ``mkdir(parents=True)`` follows a symlinked parent; the parent-dir guard
        replaces the symlinked ``.github`` with a real directory so the workflow
        lands inside the repo and never in the link's target.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".github").symlink_to(outside, target_is_directory=True)

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        # Nothing escaped into the symlink target.
        assert not (outside / "workflows").exists()

    def test_symlinked_workflows_parent_cannot_escape(self, tmp_path: Path) -> None:
        """Same guard one level deeper: a symlinked ``.github/workflows``."""
        repo = tmp_path / "repo"
        (repo / ".github").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".github" / "workflows").symlink_to(outside, target_is_directory=True)

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github" / "workflows").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        assert not (outside / _WORKFLOW_NAME).exists()


# ── remove_ci_workflow ─────────────────────────────────────────────


class TestRemoveCiWorkflow:
    """CI workflow removal."""

    def test_removes_workflow(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert remove_ci_workflow(repo) is True
        assert not (repo / ".github" / "workflows" / _WORKFLOW_NAME).exists()

    def test_no_workflow_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert remove_ci_workflow(repo) is False

    def test_no_repo_root_returns_false(self) -> None:
        assert remove_ci_workflow(Path("/nonexistent")) is False


# ── check_ci_workflow ──────────────────────────────────────────────


class TestCheckCiWorkflow:
    """CI workflow presence and currency checks."""

    def test_current_returns_true(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_ci_workflow(repo)
        assert check_ci_workflow(repo) is True

    def test_missing_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert check_ci_workflow(repo) is False

    def test_stale_returns_false(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        target = repo / ".github" / "workflows" / _WORKFLOW_NAME
        target.parent.mkdir(parents=True)
        target.write_text("old content")
        assert check_ci_workflow(repo) is False

    def test_no_repo_root_returns_false(self) -> None:
        assert check_ci_workflow(Path("/nonexistent")) is False
