"""Tests for ``biff._stdlib.get_repo_common_root`` (biff-ar1/om9 design §3a).

Uses real ``git init``/``git worktree add`` under ``tmp_path`` -- the same
approach ``test_git_hooks.py`` uses -- so these tests exercise the actual
``git rev-parse --git-common-dir`` resolution, not a mock of it.  Git-walk
confinement (so the resolver never climbs into the real repo) is provided
suite-wide by the autouse ``_confine_git_walk`` fixture in conftest.py.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from biff._stdlib import get_repo_common_root


def _git(repo: Path, *args: str) -> None:
    """Run a git command in *repo*, raising on failure."""
    subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )


def _make_repo(tmp_path: Path) -> Path:
    """Initialise a real git repo with one commit under tmp_path."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "init")
    return tmp_path


class TestGetRepoCommonRoot:
    """``git rev-parse --git-common-dir``'s parent, unifying worktrees."""

    def test_main_checkout(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        assert get_repo_common_root(str(repo)) == str(repo.resolve())

    def test_linked_worktree_resolves_to_main_root(self, tmp_path: Path) -> None:
        """A linked worktree's common root is the MAIN repo's path, not its own.

        This is the empirical fact the biff-ar1 fix depends on (design §2a):
        ``--git-common-dir``'s parent is identical whether resolved from the
        main checkout or from any of its linked worktrees.
        """
        repo = _make_repo(tmp_path / "repo")
        worktree = tmp_path / "repo-wt"
        _git(repo, "worktree", "add", str(worktree), "-b", "wt-branch", "HEAD")

        main_root = get_repo_common_root(str(repo))
        wt_root = get_repo_common_root(str(worktree))

        assert wt_root == main_root
        assert wt_root == str(repo.resolve())
        # And it disagrees with the nearest-toplevel view of the worktree --
        # the exact mismatch --show-toplevel produces that this fix closes.
        toplevel = subprocess.run(  # noqa: S603
            ["git", "-C", str(worktree), "rev-parse", "--show-toplevel"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert toplevel != main_root

    def test_not_a_repo_returns_empty(self, tmp_path: Path) -> None:
        empty = tmp_path / "not-a-repo"
        empty.mkdir()
        assert get_repo_common_root(str(empty)) == ""

    def test_default_cwd_uses_process_cwd(
        self, tmp_path: Path, monkeypatch: object
    ) -> None:
        """Omitting *cwd* falls back to the process's own working directory."""
        import os

        repo = _make_repo(tmp_path / "repo")
        cwd_before = Path.cwd()
        try:
            os.chdir(repo)
            assert get_repo_common_root() == str(repo.resolve())
        finally:
            os.chdir(cwd_before)

    def test_relative_git_common_dir_resolved_against_cwd(self, tmp_path: Path) -> None:
        """git prints a relative ``.git`` for the main checkout -- must resolve
        against *cwd*, not the process's own ambient cwd."""
        repo = _make_repo(tmp_path / "repo")
        # From the repo root itself, git prints the bare relative ".git".
        result = subprocess.run(  # noqa: S603
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
        )
        assert not Path(result.stdout.strip()).is_absolute()
        assert get_repo_common_root(str(repo)) == str(repo.resolve())

    def test_subprocess_timeout_degrades_to_empty(self, tmp_path: Path) -> None:
        """A hung ``git`` call must degrade to ``""`` (fail-open), not propagate.

        ``subprocess.TimeoutExpired`` is a ``SubprocessError``, NOT a
        ``TimeoutError`` -- catching ``TimeoutError`` alone silently missed
        it and the exception unwound the hook subprocess, breaking the
        invoking tool call. The catch list now includes
        ``subprocess.TimeoutExpired`` explicitly. Confirms the
        fail-open contract DES-054 records under "Amendment:
        root-resolution failure".
        """
        repo = _make_repo(tmp_path / "repo")

        def _raise_timeout(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=5)

        with patch("subprocess.run", side_effect=_raise_timeout):
            assert get_repo_common_root(str(repo)) == ""
