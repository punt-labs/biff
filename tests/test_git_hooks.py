"""Tests for git hook deployment (DES-017, biff-9z2).

Unit tests for deploy/remove/check operations on ``.git/hooks/``.
Uses ``tmp_path`` to simulate a git repo without touching the real one.
"""

from __future__ import annotations

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
)


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo structure in tmp_path."""
    hooks_dir = tmp_path / ".git" / "hooks"
    hooks_dir.mkdir(parents=True)
    return tmp_path


# ── deploy_git_hooks ───────────────────────────────────────────────


class TestDeployGitHooks:
    """Git hook deployment — create, append, idempotent update."""

    def test_creates_new_hooks(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        updated = deploy_git_hooks(repo)

        assert set(updated) == set(GIT_HOOKS)
        for name in GIT_HOOKS:
            hook = repo / ".git" / "hooks" / name
            assert hook.exists()
            content = hook.read_text()
            assert "#!/usr/bin/env bash" in content
            assert _MARKER_START in content
            assert _MARKER_END in content
            assert hook.stat().st_mode & 0o111  # executable

    def test_appends_to_existing_hook(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        existing = repo / ".git" / "hooks" / "post-commit"
        existing.write_text("#!/bin/sh\necho 'existing hook'\n")

        deploy_git_hooks(repo)

        content = existing.read_text()
        assert "existing hook" in content
        assert _MARKER_START in content

    def test_preserves_existing_content(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        beads_hook = repo / ".git" / "hooks" / "post-checkout"
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
        first = {
            name: (repo / ".git" / "hooks" / name).read_text() for name in GIT_HOOKS
        }

        updated = deploy_git_hooks(repo)
        assert updated == []  # Nothing changed

        for name in GIT_HOOKS:
            assert (repo / ".git" / "hooks" / name).read_text() == first[name]

    def test_no_git_dir_returns_empty(self, tmp_path: Path) -> None:
        assert deploy_git_hooks(tmp_path) == []

    def test_no_repo_root_returns_empty(self) -> None:
        assert deploy_git_hooks(Path("/nonexistent")) == []


# ── remove_git_hooks ──────────────────────────────────────────────


class TestRemoveGitHooks:
    """Git hook removal — clean up biff blocks."""

    def test_removes_biff_only_hooks(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        deploy_git_hooks(repo)

        removed = remove_git_hooks(repo)
        assert set(removed) == set(GIT_HOOKS)

        for name in GIT_HOOKS:
            assert not (repo / ".git" / "hooks" / name).exists()

    def test_preserves_other_content(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        hook = repo / ".git" / "hooks" / "post-commit"
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
        (repo / ".git" / "hooks" / "pre-push").unlink()
        missing = check_git_hooks(repo)
        assert missing == ["pre-push"]


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
