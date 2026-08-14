"""Tests for ``biff._stdlib.get_repo_common_root``.

Uses real ``git init``/``git worktree add`` under ``tmp_path`` -- the same
approach ``test_git_hooks.py`` uses -- so these tests exercise the actual
``git rev-parse --git-common-dir`` resolution, not a mock of it.  Git-walk
confinement (so the resolver never climbs into the real repo) is provided
suite-wide by the autouse ``_confine_git_walk`` fixture in conftest.py.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

from biff._stdlib import get_repo_common_root

if TYPE_CHECKING:
    import pytest


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

        This is the empirical fact the plan-gate scoping fix depends on:
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

    def test_default_cwd_uses_process_cwd(self, tmp_path: Path) -> None:
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


class TestGetRepoCommonRootLogging:
    """Failure modes are logged at the level DES-054 amendment names.

    Silence was the exact bug this branch's history keeps circling back
    to: the shared 'default' hint bucket cross-contaminates markers
    across repos for as long as git resolution fails, and until this
    round the failure was invisible in logs.
    """

    def test_git_missing_logs_debug(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A missing ``git`` binary is the benign common case -- debug, not warning."""

        def _raise_fnf(*_args: object, **_kwargs: object) -> object:
            raise FileNotFoundError(2, "No such file or directory", "git")

        with (
            caplog.at_level(logging.DEBUG, logger="biff._stdlib"),
            patch("subprocess.run", side_effect=_raise_fnf),
        ):
            assert get_repo_common_root(str(tmp_path)) == ""

        records = [r for r in caplog.records if r.name == "biff._stdlib"]
        assert len(records) == 1
        assert records[0].levelno == logging.DEBUG
        assert "git not on PATH" in records[0].getMessage()

    def test_timeout_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A hung ``git`` is an operational concern -- warn so the outage is visible."""

        def _raise_timeout(*_args: object, **_kwargs: object) -> object:
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=5)

        with (
            caplog.at_level(logging.DEBUG, logger="biff._stdlib"),
            patch("subprocess.run", side_effect=_raise_timeout),
        ):
            assert get_repo_common_root(str(tmp_path)) == ""

        records = [r for r in caplog.records if r.name == "biff._stdlib"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "timed out" in records[0].getMessage()

    def test_nonzero_exit_logs_stderr(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Non-zero git exit is warned with git's own ``stderr`` for triage."""
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        with caplog.at_level(logging.DEBUG, logger="biff._stdlib"):
            assert get_repo_common_root(str(not_a_repo)) == ""

        records = [r for r in caplog.records if r.name == "biff._stdlib"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        msg = records[0].getMessage()
        assert "git rev-parse exited" in msg
        assert "not a git repository" in msg.lower()

    def test_os_error_logs_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A generic ``OSError`` (permission blip, EIO) surfaces at warning."""

        def _raise_os(*_args: object, **_kwargs: object) -> object:
            raise PermissionError(13, "Permission denied", "git")

        with (
            caplog.at_level(logging.DEBUG, logger="biff._stdlib"),
            patch("subprocess.run", side_effect=_raise_os),
        ):
            assert get_repo_common_root(str(tmp_path)) == ""

        records = [r for r in caplog.records if r.name == "biff._stdlib"]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert "OSError" in records[0].getMessage()
