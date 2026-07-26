"""Tests for enablement (``RepoEnablement``).

``RepoEnablement`` is the single definition of what ``enable``/``disable`` do,
so both front-ends (the CLI verbs and the MCP ``biff`` tool) produce an
identical fully-active clone (DES-052, biff-j5u).  ``enable`` writes three
artifacts -- the committed marker, the committed CI workflow, and this clone's
local ``.git/hooks`` dispatchers -- and ``disable`` removes exactly those.

A real ``git init`` is used so ``git_hooks.resolve_hooks_dir`` (which asks
``git rev-parse``) sees a genuine repository; the suite-wide
``_confine_git_walk`` fixture (conftest) keeps the resolver from climbing into
the enclosing project repo.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest

from biff.enablement import RepoEnablement
from biff.git_hooks import GIT_HOOKS, resolve_hooks_dir

if TYPE_CHECKING:
    from pathlib import Path


def _make_repo(root: Path) -> Path:
    """Initialise a real git repo at *root* (needed by the hooks resolver)."""
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "init", "-q"],  # noqa: S607
        check=True,
        capture_output=True,
    )
    return root


def _hooks_dir(root: Path) -> Path:
    resolved = resolve_hooks_dir(root)
    assert resolved is not None
    return resolved


class TestEnable:
    def test_writes_marker(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".punt-labs" / "biff" / "enabled").is_file()

    def test_writes_ci_workflow(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".github" / "workflows" / "biff-notify.yml").is_file()

    def test_deploys_git_hooks(self, tmp_path: Path) -> None:
        """Enable fully activates the clone: the local git hooks land too."""
        _make_repo(tmp_path)
        change = RepoEnablement(tmp_path).enable()
        assert set(change.git_hooks_changed) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            assert (_hooks_dir(tmp_path) / name).is_file()

    def test_idempotent(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        RepoEnablement(tmp_path).enable()
        assert (tmp_path / ".punt-labs" / "biff" / "enabled").is_file()
        assert (tmp_path / ".github" / "workflows" / "biff-notify.yml").is_file()
        for name in GIT_HOOKS:
            assert (_hooks_dir(tmp_path) / name).is_file()

    def test_reports_changes(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        first = RepoEnablement(tmp_path).enable()
        assert first.ci_workflow_changed is True
        assert set(first.git_hooks_changed) == set(GIT_HOOKS)
        second = RepoEnablement(tmp_path).enable()
        assert second.ci_workflow_changed is False  # already up to date
        assert second.git_hooks_changed == ()  # hooks already deployed

    def test_ci_failure_leaves_repo_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed CI-workflow write must not leave the marker behind.

        The marker is written last, so if deploying the CI workflow raises,
        ``is_enabled`` still reads absent and the repo stays OFF (fail-safe)
        rather than half-enabled.
        """
        _make_repo(tmp_path)

        def boom(_root: Path) -> bool:
            raise OSError("disk full")

        monkeypatch.setattr("biff.enablement.deploy_ci_workflow", boom)

        with pytest.raises(OSError, match="disk full"):
            RepoEnablement(tmp_path).enable()

        assert not (tmp_path / ".punt-labs" / "biff" / "enabled").exists()

    def test_hook_failure_leaves_repo_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed git-hook deploy must not leave the marker behind either."""
        _make_repo(tmp_path)

        def boom(_root: Path) -> list[str]:
            raise OSError("hooks unwritable")

        monkeypatch.setattr("biff.enablement.deploy_git_hooks", boom)

        with pytest.raises(OSError, match="hooks unwritable"):
            RepoEnablement(tmp_path).enable()

        assert not (tmp_path / ".punt-labs" / "biff" / "enabled").exists()


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

    def test_removes_git_hooks(self, tmp_path: Path) -> None:
        """Disable removes exactly what enable added, including the git hooks."""
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).enable()
        change = RepoEnablement(tmp_path).disable()
        assert set(change.git_hooks_changed) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            assert not (_hooks_dir(tmp_path) / name).exists()

    def test_idempotent(self, tmp_path: Path) -> None:
        _make_repo(tmp_path)
        RepoEnablement(tmp_path).disable()
        RepoEnablement(tmp_path).disable()
        assert not (tmp_path / ".punt-labs" / "biff" / "enabled").exists()


class TestSurfaceEquivalence:
    """CLI ``enable`` and MCP ``enable`` leave the SAME fully-active state."""

    def test_enable_state_is_identical(self, tmp_path: Path) -> None:
        cli_repo = _make_repo(tmp_path / "cli")
        mcp_repo = _make_repo(tmp_path / "mcp")

        # Both surfaces call the same RepoEnablement definition.
        RepoEnablement(cli_repo).enable()
        RepoEnablement(mcp_repo).enable()

        def state(root: Path) -> tuple[bool, bool, frozenset[str]]:
            marker = (root / ".punt-labs" / "biff" / "enabled").is_file()
            ci = (root / ".github" / "workflows" / "biff-notify.yml").is_file()
            hooks = frozenset(
                name for name in GIT_HOOKS if (_hooks_dir(root) / name).is_file()
            )
            return marker, ci, hooks

        assert state(cli_repo) == state(mcp_repo)
        assert state(cli_repo) == (True, True, frozenset(GIT_HOOKS))
