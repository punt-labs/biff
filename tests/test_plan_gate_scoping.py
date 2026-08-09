"""Regression tests for biff-ar1 / biff-om9 -- the plan-gate marker scoping fix.

Each class reproduces one of the design's documented real-world occurrences
against a real git repo (main checkout + linked worktree), not a mock of
git, and shows the fix closes it:

- ``TestWorktreeVsMainRootMismatch`` -- ar1 occurrence 1 (a session in a
  linked worktree and the MCP writer resolved different absolute paths).
- ``TestSubagentCwdDiffersFromAmbientCwd`` -- ar1 occurrence 5 / biff-if2
  (a dispatched subagent's hook subprocess inherits its outer session's
  ambient cwd, not its own).
- ``TestConcurrentSessionStartDoesNotClearSibling`` -- om9's core mechanism
  (one session's ``SessionStart`` wiping every concurrent session's marker).
- ``TestCliOnlyPlanSatisfiesGate`` -- PL-PA-3 (a CLI-only session's ``biff
  plan`` never wrote the marker the gate reads).
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.cli_session import CliContext
from biff.commands.plan import plan
from biff.hook import handle_pre_tool_use, handle_session_start
from biff.markers import has_plan_marker, write_plan_marker
from biff.models import BiffConfig
from biff.relay import LocalRelay


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


class TestWorktreeVsMainRootMismatch:
    """ar1 occurrence 1: a linked-worktree session vs. the main-rooted writer."""

    def test_marker_written_from_worktree_visible_from_main(
        self, tmp_path: Path
    ) -> None:
        """A marker written using the worktree's own root reads back from main.

        Before the fix, the writer resolved ``--show-toplevel`` from the
        worktree (its own nearest root) while a main-rooted reader resolved
        a *different* nearest root -- two different hash buckets, so the
        main-rooted reader saw no marker at all.  Both sides now resolve
        the shared repo-common-root.
        """
        from biff._stdlib import get_repo_common_root

        main = _make_repo(tmp_path / "main")
        worktree = tmp_path / "wt"
        _git(main, "worktree", "add", str(worktree), "-b", "wt-branch", "HEAD")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            root_from_worktree = get_repo_common_root(str(worktree))
            write_plan_marker(root_from_worktree, "sid-1", "worktree plan")

            root_from_main = get_repo_common_root(str(main))
            assert has_plan_marker(root_from_main, "sid-1")


class TestSubagentCwdDiffersFromAmbientCwd:
    """ar1 occurrence 5 / biff-if2: hook's ambient cwd != its delivered cwd."""

    def test_hook_uses_delivered_cwd_not_ambient_process_cwd(
        self, tmp_path: Path
    ) -> None:
        """The gate resolves the worktree from data['cwd'], not os.getcwd().

        Simulates a dispatched subagent's own ``PreToolUse`` call: the hook
        subprocess's ambient cwd is the OUTER session's directory (an
        unrelated repo), but the payload's own ``cwd`` field names the
        subagent's actual worktree.  A gate that trusted only the ambient
        cwd would resolve the wrong repo (or none at all) and deny an edit
        whose plan really is set -- exactly the biff-if2 manifestation.
        """
        subagent_repo = _make_repo(tmp_path / "subagent-repo")
        outer_repo = _make_repo(tmp_path / "outer-repo")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            root = get_repo_common_root(str(subagent_repo))
            write_plan_marker(root, "sub-sid", "subagent's own plan")

            # The hook process's ambient cwd is the OUTER repo -- but its
            # delivered payload names the subagent's own worktree.
            with patch("pathlib.Path.cwd", return_value=outer_repo):
                result = handle_pre_tool_use(
                    {
                        "session_id": "sub-sid",
                        "cwd": str(subagent_repo),
                    }
                )

        assert result is None  # allowed -- cwd threading found the right marker

    def test_ambient_cwd_alone_would_have_missed_it(self, tmp_path: Path) -> None:
        """Sanity check: without cwd-threading, the ambient repo is wrong."""
        subagent_repo = _make_repo(tmp_path / "subagent-repo")
        outer_repo = _make_repo(tmp_path / "outer-repo")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            root = get_repo_common_root(str(subagent_repo))
            write_plan_marker(root, "sub-sid", "subagent's own plan")

            with patch("pathlib.Path.cwd", return_value=outer_repo):
                # No cwd in the payload -- falls back to ambient (the outer
                # repo), which has no marker for sub-sid.
                result = handle_pre_tool_use({"session_id": "sub-sid"})

        assert result is not None  # denied -- proves the fix is load-bearing


class TestConcurrentSessionStartDoesNotClearSibling:
    """om9's core mechanism: one session's SessionStart wiping every marker."""

    def test_sibling_marker_survives(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")

        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            root = get_repo_common_root(str(repo))
            write_plan_marker(root, "sid-sibling", "sibling's plan")

            with patch("pathlib.Path.cwd", return_value=repo):
                handle_session_start({"session_id": "sid-new", "cwd": str(repo)})

            assert has_plan_marker(root, "sid-sibling")


class TestCliOnlyPlanSatisfiesGate:
    """PL-PA-3: a CLI-only 'biff plan' now satisfies the gate it advertises.

    Before this fix, ``commands/plan.py``'s ``plan()`` (the function
    ``biff plan`` calls) updated only the relay session -- it never wrote
    the marker file the ``PreToolUse`` gate reads.  Every ethos-mission
    worker in this org is a CLI-only session (no MCP tools): running
    ``biff plan "..."`` reported success while the gate kept denying every
    subsequent edit -- impossible to reproduce as a passing test before
    this fix, because the marker write path did not exist.
    """

    async def test_cli_plan_then_hook_allows_edit(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path / "repo")
        relay = LocalRelay(tmp_path / "relay-data")
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("pathlib.Path.cwd", return_value=repo),
            patch(
                "biff.commands.plan.SessionHint.resolve_routing_id",
                return_value="cli-session-id",
            ),
        ):
            result = await plan(ctx, "biff-ar1: fix the plan gate")
            assert not result.error

            gate_result = handle_pre_tool_use(
                {"session_id": "cli-session-id", "cwd": str(repo)}
            )

        assert gate_result is None  # allowed

    async def test_cli_plan_clear_then_hook_denies(self, tmp_path: Path) -> None:
        """Clearing the CLI plan (biff plan --clear) re-denies via the same path."""
        repo = _make_repo(tmp_path / "repo")
        relay = LocalRelay(tmp_path / "relay-data")
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
        )

        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("pathlib.Path.cwd", return_value=repo),
            patch(
                "biff.commands.plan.SessionHint.resolve_routing_id",
                return_value="cli-session-id",
            ),
        ):
            await plan(ctx, "a plan")
            await plan(ctx, "")  # clear

            gate_result = handle_pre_tool_use(
                {"session_id": "cli-session-id", "cwd": str(repo)}
            )

        assert gate_result is not None  # denied


@pytest.fixture(autouse=True)
def _active_session(  # pyright: ignore[reportUnusedFunction]
    tmp_path: Path,
) -> Iterator[None]:
    """Fake an active biff MCP session so the gate doesn't graceful-allow.

    ``handle_pre_tool_use`` returns ``None`` unconditionally when
    ``_has_active_session()`` is false (DES-051's own graceful-allow
    escape hatch) -- these tests exercise the *marker* logic downstream of
    that check, so an active session must be simulated.
    """
    active = tmp_path / "active-marker-dir"
    active.mkdir(parents=True, exist_ok=True)
    (active / "someone").write_text("someone:tty\nrepo\n")
    with patch("biff._stdlib.active_dir", return_value=active):
        yield
