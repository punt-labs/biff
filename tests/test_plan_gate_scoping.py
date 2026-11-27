"""Test repository-common plan-marker scoping against real git worktrees.

Writers and readers must share the common-root bucket, subagents must use their
declared working directory, concurrent session starts must preserve sibling
markers, and CLI-only plan updates must satisfy the edit gate.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from biff.cli_context import CliContext
from biff.dispatch import dispatch
from biff.markers import has_plan_marker, write_plan_marker
from biff.models import BiffConfig
from biff.plugins.claude_code.hook import handle_pre_tool_use, handle_session_start
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
        """A worktree writer and main-rooted reader share the common-root bucket."""
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

        assert (
            result is not None
        )  # denied -- proves the gate enforces the marker requirement


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
    """A CLI-only plan update writes the marker consumed by the edit gate.

    CLI workers do not have MCP tools, so successful relay updates must also
    make their plan visible to PreToolUse.
    """

    async def test_cli_plan_then_hook_allows_edit(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cli-session-id")
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
            result = await dispatch("plan biff-ar1: fix the plan gate", ctx)
            assert not result.error

            gate_result = handle_pre_tool_use(
                {"session_id": "cli-session-id", "cwd": str(repo)}
            )

        assert gate_result is None  # allowed

    async def test_cli_plan_clear_then_hook_denies(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Clearing the CLI plan (biff plan --clear) re-denies via the same path."""
        repo = _make_repo(tmp_path / "repo")
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "cli-session-id")
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
            await dispatch("plan a plan", ctx)
            await dispatch("plan clear", ctx)

            gate_result = handle_pre_tool_use(
                {"session_id": "cli-session-id", "cwd": str(repo)}
            )

        assert gate_result is not None  # denied


class TestUnresolvedRootDoesNotWedge:
    """Allow edits when a transient git failure leaves the root unresolved.

    A missing repository root must not accidentally match every file path.
    """

    def test_empty_root_allows_rather_than_denies(self, tmp_path: Path) -> None:
        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("biff.plugins.claude_code.hook._repo_common_root", return_value=""),
        ):
            result = handle_pre_tool_use({"session_id": "sid", "cwd": "/not/a/repo"})

        assert result is None  # allowed -- no repo root to gate against

    def test_resolved_root_without_marker_still_denies(self, tmp_path: Path) -> None:
        """The empty-root allow must not leak into the resolved-root path.

        Paired with the empty-root case: with a *resolved* root and no plan
        marker for any of this caller's identities, the gate still denies.
        This pins that the marker suppresses the edit requirement only when the
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
        from biff.plan_marker import PlanMarker

        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            PlanMarker.sync("declared plan", "declared-session")
            allowed = handle_pre_tool_use(
                {"session_id": "declared-session", "cwd": str(repo)}
            )
            sibling = handle_pre_tool_use(
                {"session_id": "sibling-session", "cwd": str(repo)}
            )

        assert allowed is None
        assert sibling is not None


class TestSharedBucketDoesNotAuthorizeASession:
    """An unscoped plan must not satisfy another session's edit gate."""

    def test_shared_marker_does_not_satisfy_an_identified_session(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            write_plan_marker(get_repo_common_root(str(repo)), None, "cli plan")
            result = handle_pre_tool_use(
                {"session_id": "other-session", "cwd": str(repo)}
            )
        assert result is not None  # denied

    def test_shared_marker_still_serves_a_payload_with_no_identity(
        self, tmp_path: Path
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            write_plan_marker(get_repo_common_root(str(repo)), None, "headless")
            assert handle_pre_tool_use({"cwd": str(repo)}) is None


class TestCliPlanIsScopedToTheEnvironmentSession:
    def test_cli_plan_writes_under_the_session_and_reset_clears_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from biff.plan_marker import PlanMarker

        repo = _make_repo(tmp_path / "repo")
        monkeypatch.chdir(repo)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "my-session")
        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            patch("pathlib.Path.cwd", return_value=repo),
        ):
            from biff._stdlib import get_repo_common_root

            root = get_repo_common_root(str(repo))
            PlanMarker.sync("cli plan")
            assert has_plan_marker(root, "my-session")
            assert not has_plan_marker(root, None)
            assert handle_pre_tool_use({"session_id": "other", "cwd": str(repo)})
            handle_session_start({"session_id": "my-session", "cwd": str(repo)})
            assert not has_plan_marker(root, "my-session")

    def test_a_malformed_environment_session_falls_to_the_shared_bucket(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from biff.plan_marker import PlanMarker

        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "../evil")
        assert PlanMarker.declared_session() is None
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
        assert PlanMarker.declared_session() is None


class TestSkippedSharedPlanIsDiagnosable:
    def test_logs_when_only_a_shared_marker_exists_for_an_identified_caller(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        with patch("pathlib.Path.home", return_value=tmp_path / "home"):
            from biff._stdlib import get_repo_common_root

            write_plan_marker(get_repo_common_root(str(repo)), None, "cli plan")
            with caplog.at_level("INFO", logger="biff.plugins.claude_code.hook"):
                result = handle_pre_tool_use(
                    {"session_id": "skewed-session", "cwd": str(repo)}
                )
        assert result is not None  # decision unchanged: still denied
        assert any(
            "identified session" in r.getMessage() and "shared" in r.getMessage()
            for r in caplog.records
        )
        assert "skewed-session" not in caplog.text

    def test_no_log_without_a_shared_marker(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        repo = _make_repo(tmp_path / "repo")
        with (
            patch("pathlib.Path.home", return_value=tmp_path / "home"),
            caplog.at_level("INFO", logger="biff.plugins.claude_code.hook"),
        ):
            handle_pre_tool_use({"session_id": "s", "cwd": str(repo)})
        assert not any("shared" in r.getMessage() for r in caplog.records)
