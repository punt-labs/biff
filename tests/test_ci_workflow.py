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


# A resolvable, non-dead action pin. The prior pin
# (e58605a9b6da7c637471fab8847a5e5a6b8df081) does not exist on GitHub — the
# notify job failed to resolve the action on first trigger in every repo.
_LIVE_SETUP_UV_SHA = "c771a70e6277c0a99b617c7a806ffedaca235ff9"
_DEAD_SETUP_UV_SHA = "e58605a9b6da7c637471fab8847a5e5a6b8df081"


# ── template action pins ───────────────────────────────────────────


class TestTemplateActionPins:
    """The bundled template must pin only resolvable action SHAs."""

    def test_setup_uv_pinned_to_live_sha(self) -> None:
        content = _template_content()
        assert _LIVE_SETUP_UV_SHA in content
        assert _DEAD_SETUP_UV_SHA not in content


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

    def test_symlinked_github_no_fail_open_when_target_resolves(
        self, tmp_path: Path
    ) -> None:
        """Fail-open regression: ``.github`` is a symlink AND the full nested
        path already resolves through it.

        An ``exists()`` short-circuit would see ``.github/workflows`` resolve
        and skip the guard, letting the write follow the link out of the repo.
        The guard must still replace the symlinked ``.github`` and keep the
        write inside.
        """
        repo = tmp_path / "repo"
        repo.mkdir()
        outside = tmp_path / "outside"
        (outside / "workflows").mkdir(parents=True)  # nested path pre-exists
        (repo / ".github").symlink_to(outside, target_is_directory=True)
        # repo/.github/workflows resolves (through the link) to an existing dir.
        assert (repo / ".github" / "workflows").is_dir()

        assert deploy_ci_workflow(repo) is True

        assert not (repo / ".github").is_symlink()
        assert (repo / ".github" / "workflows" / _WORKFLOW_NAME).is_file()
        # The workflow never landed in the symlink target.
        assert not (outside / "workflows" / _WORKFLOW_NAME).exists()


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

    def test_directory_at_workflow_path_left_alone(self, tmp_path: Path) -> None:
        """A directory (not ours) at the workflow path: report False, don't touch."""
        repo = tmp_path / "repo"
        wf = repo / ".github" / "workflows" / _WORKFLOW_NAME
        wf.mkdir(parents=True)  # a directory occupies the workflow path

        assert remove_ci_workflow(repo) is False
        assert wf.is_dir()  # left untouched, not misreported as removed

    def test_symlinked_workflow_removed_without_touching_target(
        self, tmp_path: Path
    ) -> None:
        """A committed symlinked workflow: remove the link, never its target."""
        repo = tmp_path / "repo"
        outside = tmp_path / "secret.txt"
        outside.write_text("keep me")
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / _WORKFLOW_NAME).symlink_to(outside)

        assert remove_ci_workflow(repo) is True
        assert not (wf / _WORKFLOW_NAME).exists()  # the link is gone
        assert outside.read_text() == "keep me"  # target untouched


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

    def test_symlinked_workflow_not_current(self, tmp_path: Path) -> None:
        """A symlinked workflow path is not ours — not-current, never followed.

        The link's target matches the template, yet check must not follow the
        symlink to read it; it reports the workflow as not deployed/current.
        """
        repo = tmp_path / "repo"
        outside = tmp_path / "tpl.yml"
        outside.write_text(_template_content())
        wf = repo / ".github" / "workflows"
        wf.mkdir(parents=True)
        (wf / _WORKFLOW_NAME).symlink_to(outside)

        assert check_ci_workflow(repo) is False
