"""Regression tests for the plan-gate marker scoping fix.

Each class reproduces one of the design's documented real-world occurrences
against a real git repo (main checkout + linked worktree), not a mock of
git, and shows the fix closes it:

- ``TestWorktreeVsMainRootMismatch`` -- a session in a
  linked worktree and the MCP writer resolved different absolute paths.
- ``TestSubagentCwdDiffersFromAmbientCwd`` -- a dispatched subagent's hook
  subprocess inherits its outer session's ambient cwd, not its own.
- ``TestConcurrentSessionStartDoesNotClearSibling`` -- one session's
  ``SessionStart`` wiping every concurrent session's marker.
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
    """Hook's ambient cwd != its delivered cwd."""

    def test_hook_uses_delivered_cwd_not_ambient_process_cwd(
        self, tmp_path: Path
    ) -> None:
        """The gate resolves the worktree from data['cwd'], not os.getcwd().

        Simulates a dispatched subagent's own ``PreToolUse`` call: the hook
        subprocess's ambient cwd is the OUTER session's directory (an
        unrelated repo), but the payload's own ``cwd`` field names the
        subagent's actual worktree.  A gate that trusted only the ambient
        cwd would resolve the wrong repo (or none at all) and deny an edit
        whose plan really is set -- the exact failure mode this test
        reproduces.
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

    async def test_cli_plan_then_hook_allows_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        # plan() shells out to git in the process cwd; patching Path.cwd does
        # not reach the subprocess, so a linked-worktree test run resolved the
        # marker root to the real checkout instead of this temp repo.
        monkeypatch.chdir(repo)
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
        ):
            result = await plan(ctx, "biff-ar1: fix the plan gate")
            assert not result.error

            gate_result = handle_pre_tool_use(
                {"session_id": "some-other-session", "cwd": str(repo)}
            )

        assert gate_result is None  # allowed

    async def test_cli_plan_clear_then_hook_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing the CLI plan (biff plan --clear) re-denies via the same path."""
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
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
        ):
            await plan(ctx, "a plan")
            await plan(ctx, "")  # clear

            gate_result = handle_pre_tool_use(
                {"session_id": "some-other-session", "cwd": str(repo)}
            )

        assert gate_result is not None  # denied


class TestUnresolvedRootDoesNotWedge:
    """biff-ecl / PL-240: a transient git failure must not deny every edit.

    When ``get_repo_common_root`` returns ``""`` (git unavailable, timed
    out, or a cwd that is briefly not a repo), the gate cannot know *which*
    repo's plan to consult -- every unresolved root collapses onto the one
    shared ``default`` bucket. Reading that bucket and denying turns a
    bookkeeping outage into a block on the whole editing surface, and (via
    the shared bucket) lets one repo's plan satisfy another's gate. With no
    resolvable repo the gate has no marker store to reason about, so it
    allows -- the same graceful escape hatch as "no active session" -- and
    the ``hook-errors.log`` warning from ``hint_dir`` records the cause.
    """

    def test_empty_root_allows_rather_than_denies(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("biff.hook._repo_common_root", return_value=""),
        ):
            result = handle_pre_tool_use({"session_id": "sid", "cwd": "/not/a/repo"})

        assert result is None  # allowed -- no repo root to gate against

    def test_resolved_root_without_marker_still_denies(self, tmp_path: Path) -> None:
        """The empty-root allow must not leak into the resolved-root path.

        Paired with the empty-root case: with a *resolved* root and no plan
        marker for any of this caller's identities, the gate still denies.
        This pins that the fix suppresses the edit requirement only when the
        root is unresolvable, never when it resolves to a real repo with no
        plan.
        """
        repo = _make_repo(tmp_path / "repo")

        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("pathlib.Path.cwd", return_value=repo),
        ):
            result = handle_pre_tool_use({"session_id": "sid", "cwd": str(repo)})

        assert result is not None  # denied -- resolved root, no plan


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


class TestDeclaredSessionPlan:
    """The MCP plan tool keys the marker on the session the caller declared."""

    def test_marker_lands_under_the_declared_session_and_satisfies_its_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from biff.commands.plan import sync_plan_marker

        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            sync_plan_marker("declared plan", "declared-session")
            allowed = handle_pre_tool_use(
                {"session_id": "declared-session", "cwd": str(repo)}
            )
            sibling = handle_pre_tool_use(
                {"session_id": "sibling-session", "cwd": str(repo)}
            )

        assert allowed is None
        assert sibling is not None
