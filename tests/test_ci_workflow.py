"""Tests for CI workflow deployment.

Unit tests for deploy/remove/check operations on
``.github/workflows/biff-notify.yml``.
"""

from __future__ import annotations

from pathlib import Path

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

    def test_no_repo_root_returns_false(self) -> None:
        assert deploy_ci_workflow(Path("/nonexistent")) is False


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
