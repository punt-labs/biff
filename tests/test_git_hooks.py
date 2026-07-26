"""Tests for git hook deployment (DES-017, biff-9z2).

Unit tests for deploy/remove/check operations on the resolved git hooks
directory.  Uses a real ``git init`` under ``tmp_path`` so the hooks-dir
resolver (``git rev-parse --git-path hooks``) sees a genuine repository --
the same lookup git itself performs, which honors linked worktrees,
submodules, and ``core.hooksPath``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from biff.git_hooks import (
    _MARKER_END,
    _MARKER_START,
    GIT_HOOKS,
    _has_biff_block,
    _remove_biff_block,
    check_git_hooks,
    deploy_git_hooks,
    remove_git_hooks,
    resolve_hooks_dir,
)

# Git-walk confinement (so the resolver never climbs into the real repo) is
# provided suite-wide by the autouse ``_confine_git_walk`` fixture in conftest.


def _git(repo: Path, *args: str) -> None:
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialise a real git repo with one commit under tmp_path.

    A commit is required so ``git worktree add`` (used by the worktree
    tests) has a HEAD to check out.
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "init")
    return tmp_path


def _hooks_dir(repo: Path) -> Path:
    """The resolved hooks dir for a plain (non-worktree) repo."""
    return repo / ".git" / "hooks"


# ── resolve_hooks_dir ──────────────────────────────────────────────


class TestResolveHooksDir:
    """The resolver asks git, so it honors worktrees and core.hooksPath."""

    def test_plain_repo(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert resolve_hooks_dir(repo) == _hooks_dir(repo)

    def test_not_a_git_repo(self, tmp_path: Path) -> None:
        assert resolve_hooks_dir(tmp_path) is None

    def test_nonexistent_path(self) -> None:
        assert resolve_hooks_dir(Path("/nonexistent")) is None

    def test_core_hooks_path_relative(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _git(repo, "config", "core.hooksPath", "myhooks")
        assert resolve_hooks_dir(repo) == repo / "myhooks"

    def test_core_hooks_path_absolute(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        abs_hooks = tmp_path / "shared-hooks"
        _git(repo, "config", "core.hooksPath", str(abs_hooks))
        assert resolve_hooks_dir(repo) == abs_hooks

    def test_linked_worktree_resolves_to_common_dir(self, tmp_path: Path) -> None:
        """In a linked worktree ``.git`` is a file; hooks live in the main repo."""
        main = _make_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", "--detach", "-q", str(wt))

        # The linked worktree's ``.git`` is a FILE, not a directory.
        assert (wt / ".git").is_file()

        resolved = resolve_hooks_dir(wt)
        assert resolved is not None
        # Compare resolved forms: git records an absolute path that may use a
        # different symlink spelling than tmp_path (e.g. /private on macOS).
        assert resolved.resolve() == (main / ".git" / "hooks").resolve()


# ── deploy_git_hooks ───────────────────────────────────────────────


class TestDeployGitHooks:
    """Git hook deployment — create, append, idempotent update."""

    def test_creates_new_hooks(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        updated = deploy_git_hooks(repo)

        assert set(updated) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            hook = _hooks_dir(repo) / name
            assert hook.exists()
            content = hook.read_text()
            assert "#!/usr/bin/env bash" in content
            assert _MARKER_START in content
            assert _MARKER_END in content
            assert hook.stat().st_mode & 0o111  # executable

    def test_appends_to_existing_hook(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        existing = _hooks_dir(repo) / "post-commit"
        existing.write_text("#!/bin/sh\necho 'existing hook'\n")

        deploy_git_hooks(repo)

        content = existing.read_text()
        assert "existing hook" in content
        assert _MARKER_START in content

    def test_ensures_executable_on_existing(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        hook = _hooks_dir(repo) / "post-commit"
        hook.write_text("#!/bin/sh\necho 'hello'\n")
        hook.chmod(0o644)  # Not executable

        deploy_git_hooks(repo)

        assert hook.stat().st_mode & 0o111  # Now executable

    def test_preserves_existing_content(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        beads_hook = _hooks_dir(repo) / "post-checkout"
        beads_content = "#!/bin/sh\n# beads post-checkout\nbd import\n"
        beads_hook.write_text(beads_content)

        deploy_git_hooks(repo)

        content = beads_hook.read_text()
        assert "beads post-checkout" in content
        assert "bd import" in content
        assert _MARKER_START in content

    def test_idempotent(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_git_hooks(repo)
        first = {name: (_hooks_dir(repo) / name).read_text() for name in GIT_HOOKS}

        updated = deploy_git_hooks(repo)
        assert updated == []  # Nothing changed

        for name in GIT_HOOKS:
            assert (_hooks_dir(repo) / name).read_text() == first[name]

    def test_no_git_dir_returns_empty(self, tmp_path: Path) -> None:
        assert deploy_git_hooks(tmp_path) == []

    def test_no_repo_root_returns_empty(self) -> None:
        assert deploy_git_hooks(Path("/nonexistent")) == []

    def test_deploys_into_worktree_common_dir(self, tmp_path: Path) -> None:
        """A linked worktree deploys hooks to the main repo, never <wt>/.git/hooks."""
        main = _make_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", "--detach", "-q", str(wt))

        updated = deploy_git_hooks(wt)

        assert set(updated) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            assert (main / ".git" / "hooks" / name).exists()
        # Nothing must be written to the worktree's literal .git/hooks path.
        assert (wt / ".git").is_file()
        assert not (wt / ".git" / "hooks").exists()

    def test_deploys_into_core_hooks_path(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        _git(repo, "config", "core.hooksPath", "custom")

        updated = deploy_git_hooks(repo)

        assert set(updated) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            assert (repo / "custom" / name).exists()
        # The default .git/hooks must not receive the biff block.
        for name in GIT_HOOKS:
            default_hook = repo / ".git" / "hooks" / name
            assert not default_hook.exists() or _MARKER_START not in (
                default_hook.read_text()
            )


# ── remove_git_hooks ──────────────────────────────────────────────


class TestRemoveGitHooks:
    """Git hook removal — clean up biff blocks."""

    def test_removes_biff_only_hooks(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_git_hooks(repo)

        removed = remove_git_hooks(repo)
        assert set(removed) == set(GIT_HOOKS)

        for name in GIT_HOOKS:
            assert not (_hooks_dir(repo) / name).exists()

    def test_preserves_other_content(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        hook = _hooks_dir(repo) / "post-commit"
        hook.write_text("#!/bin/sh\necho 'keep me'\n")

        deploy_git_hooks(repo)
        remove_git_hooks(repo)

        assert hook.exists()
        content = hook.read_text()
        assert "keep me" in content
        assert _MARKER_START not in content

    def test_no_hooks_returns_empty(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        assert remove_git_hooks(repo) == []

    def test_no_git_dir_returns_empty(self, tmp_path: Path) -> None:
        assert remove_git_hooks(tmp_path) == []

    def test_removes_from_worktree_common_dir(self, tmp_path: Path) -> None:
        main = _make_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", "--detach", "-q", str(wt))
        deploy_git_hooks(wt)

        removed = remove_git_hooks(wt)
        assert set(removed) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            assert not (main / ".git" / "hooks" / name).exists()


# ── check_git_hooks ───────────────────────────────────────────────


class TestCheckGitHooks:
    """Check for missing biff git hooks."""

    def test_all_missing(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        missing = check_git_hooks(repo)
        assert set(missing) == set(GIT_HOOKS)

    def test_none_missing(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_git_hooks(repo)
        assert check_git_hooks(repo) == []

    def test_partial_missing(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_git_hooks(repo)
        (_hooks_dir(repo) / "pre-push").unlink()
        missing = check_git_hooks(repo)
        assert missing == ["pre-push"]

    def test_reads_worktree_common_dir(self, tmp_path: Path) -> None:
        """check() reads the same resolved dir deploy() wrote to."""
        main = _make_repo(tmp_path / "main")
        wt = tmp_path / "wt"
        _git(main, "worktree", "add", "--detach", "-q", str(wt))
        deploy_git_hooks(wt)

        assert check_git_hooks(wt) == []


# ── _has_biff_block / _remove_biff_block ──────────────────────────


class TestBlockHelpers:
    """Block detection and removal helpers."""

    def test_has_biff_block_true(self) -> None:
        content = f"#!/bin/sh\n{_MARKER_START}\ncmd\n{_MARKER_END}\n"
        assert _has_biff_block(content) is True

    def test_has_biff_block_false(self) -> None:
        assert _has_biff_block("#!/bin/sh\necho hello\n") is False

    def test_remove_biff_block(self) -> None:
        content = (
            f"#!/bin/sh\necho before\n{_MARKER_START}\ncmd\n{_MARKER_END}\necho after\n"
        )
        result = _remove_biff_block(content)
        assert "echo before" in result
        assert "echo after" in result
        assert _MARKER_START not in result
        assert "cmd" not in result

    def test_remove_no_block(self) -> None:
        content = "#!/bin/sh\necho hello\n"
        assert _remove_biff_block(content) == content
