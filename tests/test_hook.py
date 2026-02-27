"""Tests for biff hook dispatcher (DES-017).

Unit tests for the pure handler functions.  These test business logic
without I/O — no stdin/stdout mocking, no git repo, no .biff files.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import patch

from biff.hook import (
    _detect_collisions,
    _expand_branch_plan,
    check_plan_hint,
    check_wall_hint,
    handle_post_bash,
    handle_post_checkout,
    handle_post_commit,
    handle_post_pr,
    handle_pre_push,
    handle_session_end,
    handle_session_resume,
    handle_session_start,
)

# Deterministic worktree root for hint file tests.
_FAKE_WORKTREE = "/test/worktree"
_FAKE_HINT_HASH = hashlib.sha256(_FAKE_WORKTREE.encode()).hexdigest()[:16]


def _hint_mocks(tmp_path: Path):
    """Context manager stack mocking Path.home and worktree root."""
    return (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("biff.hook._get_worktree_root", return_value=_FAKE_WORKTREE),
    )


def _hint_path(tmp_path: Path, name: str) -> Path:
    """Expected hint file path for tests."""
    return tmp_path / ".biff" / "hints" / _FAKE_HINT_HASH / name


def _identity(s: str) -> str:
    return s


# ── handle_post_bash ─────────────────────────────────────────────────


class TestHandlePostBash:
    """Bead claim detection in PostToolUse Bash handler."""

    def test_bead_claim_equals_format(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd update biff-7vp --status=in_progress"},
            "tool_response": "\u2713 Updated issue: biff-7vp",
        }
        result = handle_post_bash(data)
        assert result is not None
        assert "/plan" in result

    def test_bead_claim_space_format(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd update biff-7vp --status in_progress"},
            "tool_response": "\u2713 Updated issue: biff-7vp",
        }
        result = handle_post_bash(data)
        assert result is not None
        assert "/plan" in result

    def test_chained_command(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "cd /tmp && bd update biff-7vp --status=in_progress"
            },
            "tool_response": "\u2713 Updated issue: biff-7vp",
        }
        assert handle_post_bash(data) is not None

    def test_non_bead_command_ignored(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_response": "total 42\n...",
        }
        assert handle_post_bash(data) is None

    def test_git_command_ignored(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_response": "On branch main\n...",
        }
        assert handle_post_bash(data) is None

    def test_bd_list_ignored(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd list --status=open"},
            "tool_response": "\u2713 some output",
        }
        assert handle_post_bash(data) is None

    def test_failed_bead_claim_ignored(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd update biff-999 --status=in_progress"},
            "tool_response": "Error: issue not found",
        }
        assert handle_post_bash(data) is None

    def test_empty_input(self) -> None:
        assert handle_post_bash({}) is None

    def test_missing_tool_input(self) -> None:
        data: dict[str, object] = {"tool_name": "Bash"}
        assert handle_post_bash(data) is None

    def test_missing_command(self) -> None:
        data: dict[str, object] = {"tool_input": {}}
        assert handle_post_bash(data) is None

    def test_non_string_response(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd update biff-7vp --status=in_progress"},
            "tool_response": {"unexpected": "dict"},
        }
        assert handle_post_bash(data) is None


# ── handle_post_pr ───────────────────────────────────────────────────


class TestHandlePostPr:
    """PR create/merge detection in PostToolUse GitHub handler."""

    def test_create_pr_github_prefix(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": json.dumps({"number": 42}),
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Created PR #42" in result
        assert "feat: hook dispatcher" in result
        assert "/wall" in result

    def test_create_pr_plugin_prefix(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__plugin_github_github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": json.dumps({"number": 42}),
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Created PR #42" in result

    def test_create_pr_response_as_dict(self) -> None:
        """tool_response may arrive as dict instead of JSON string."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": {"number": 42},
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Created PR #42" in result

    def test_merge_pr_with_title(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pullNumber": 42, "commit_title": "feat: hook dispatcher"},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Merged PR #42" in result
        assert "feat: hook dispatcher" in result

    def test_merge_pr_pull_number_field(self) -> None:
        """Both pullNumber and pull_number field names are accepted."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pull_number": 42},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Merged PR #42" in result

    def test_merge_pr_no_title(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pullNumber": 42},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "/wall Merged PR #42" in result
        assert ": " not in result.split("Merged")[1]

    def test_create_pr_missing_title(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {},
            "tool_response": json.dumps({"number": 42}),
        }
        assert handle_post_pr(data) is None

    def test_create_pr_missing_number(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": "{}",
        }
        assert handle_post_pr(data) is None

    def test_merge_pr_missing_number(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {},
            "tool_response": "{}",
        }
        assert handle_post_pr(data) is None

    def test_unknown_tool(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__list_issues",
            "tool_input": {},
            "tool_response": "{}",
        }
        assert handle_post_pr(data) is None

    def test_empty_input(self) -> None:
        assert handle_post_pr({}) is None

    def test_missing_tool_input(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
        }
        assert handle_post_pr(data) is None

    def test_empty_title_rejected(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": ""},
            "tool_response": json.dumps({"number": 42}),
        }
        assert handle_post_pr(data) is None


# ── handle_session_start ────────────────────────────────────────────


class TestHandleSessionStart:
    """SessionStart(startup) handler — branch detection and nudges."""

    def test_includes_tty_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start({})
        assert "/tty" in result

    def test_includes_read_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start({})
        assert "/read" in result

    def test_branch_included_in_plan_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value="feature/auth"):
            result = handle_session_start({})
        assert "→ feature/auth" in result
        assert 'source="auto"' in result

    def test_bead_branch_expanded(self) -> None:
        with (
            patch("biff.hook._get_git_branch", return_value="jmf/biff-ka4"),
            patch(
                "biff.server.tools.plan.expand_bead_id",
                return_value="biff-ka4: post-checkout hook",
            ),
        ):
            result = handle_session_start({})
        assert "→ biff-ka4: post-checkout hook" in result

    def test_no_branch_still_returns_context(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start({})
        assert "Biff session starting" in result
        assert "/plan" in result

    def test_main_branch_included(self) -> None:
        with patch("biff.hook._get_git_branch", return_value="main"):
            result = handle_session_start({})
        assert "→ main" in result

    def test_quotes_in_branch_escaped(self) -> None:
        with patch("biff.hook._get_git_branch", return_value='feature/"quotes"'):
            result = handle_session_start({})
        assert r"\"quotes\"" in result
        assert 'source="auto"' in result


# ── handle_session_resume ───────────────────────────────────────────


class TestHandleSessionResume:
    """SessionStart(resume|compact) handler — re-orientation nudge."""

    def test_includes_read_nudge(self) -> None:
        result = handle_session_resume()
        assert "/read" in result

    def test_mentions_resume(self) -> None:
        result = handle_session_resume()
        assert "resumed" in result


# ── _expand_branch_plan ─────────────────────────────────────────────


class TestExpandBranchPlan:
    """Branch name to plan string conversion."""

    def test_plain_branch(self) -> None:
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            assert _expand_branch_plan("feature/auth") == "→ feature/auth"

    def test_bead_id_in_branch(self) -> None:
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            return_value="biff-ka4: post-checkout hook",
        ):
            result = _expand_branch_plan("jmf/biff-ka4")
        assert result == "→ biff-ka4: post-checkout hook"

    def test_bead_id_at_start(self) -> None:
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            return_value="biff-ka4: hook",
        ):
            result = _expand_branch_plan("biff-ka4-description")
        assert result == "→ biff-ka4: hook"

    def test_no_bead_id(self) -> None:
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            assert _expand_branch_plan("main") == "→ main"

    def test_expansion_failure_uses_raw_branch(self) -> None:
        """If expand_bead_id returns the ID unchanged, use the full branch."""
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("jmf/biff-xyz")
        assert result == "→ biff-xyz"

    def test_no_false_positive_on_common_branch(self) -> None:
        """Branch names like my-feature must not be truncated to my-feat."""
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("my-feature")
        assert result == "→ my-feature"

    def test_no_false_positive_fix_tests(self) -> None:
        """fix-tests must not match as a bead ID."""
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("fix-tests")
        assert result == "→ fix-tests"

    def test_no_false_positive_add_logging(self) -> None:
        with patch(
            "biff.server.tools.plan.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("add-logging")
        assert result == "→ add-logging"


# ── handle_session_end ──────────────────────────────────────────────


class TestHandleSessionEnd:
    """SessionEnd handler — active-to-sentinel conversion."""

    def test_converts_active_to_sentinel(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc12345").write_text("kai:abc12345\nmy-repo\n")

        sentinel_dir = tmp_path / "sentinels" / "my-repo"
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch("biff.config.get_repo_slug", return_value=None),
            patch(
                "biff.server.app.sentinel_dir",
                return_value=sentinel_dir,
            ),
        ):
            count = handle_session_end()

        assert count == 1
        assert (sentinel_dir / "kai-abc12345").read_text() == "kai:abc12345"
        assert not (active_dir / "kai-abc12345").exists()

    def test_empty_active_dir(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch("biff.config.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0

    def test_no_active_dir(self, tmp_path: Path) -> None:
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch("biff.config.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0

    def test_scoped_to_current_repo(self, tmp_path: Path) -> None:
        """Only cleans up sessions matching the current repo name."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-aaa").write_text("kai:aaa\nmy-repo\n")
        (active_dir / "kai-bbb").write_text("kai:bbb\nother-repo\n")

        sentinel_dir = tmp_path / "sentinels" / "my-repo"
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch("biff.config.get_repo_slug", return_value=None),
            patch(
                "biff.server.app.sentinel_dir",
                return_value=sentinel_dir,
            ),
        ):
            count = handle_session_end()

        assert count == 1
        # my-repo session cleaned up
        assert not (active_dir / "kai-aaa").exists()
        # other-repo session left untouched
        assert (active_dir / "kai-bbb").exists()

    def test_no_git_root_returns_zero(self, tmp_path: Path) -> None:
        with patch("biff.config.find_git_root", return_value=None):
            count = handle_session_end()

        assert count == 0

    def test_matches_sanitized_repo_slug(self, tmp_path: Path) -> None:
        """Active file uses sanitized slug (owner__repo), not directory name."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text("kai:abc\npunt-labs__biff\n")

        sentinel_dir = tmp_path / "sentinels" / "punt-labs__biff"
        fake_root = tmp_path / "biff"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch(
                "biff.config.get_repo_slug",
                return_value="punt-labs/biff",
            ),
            patch(
                "biff.server.app.sentinel_dir",
                return_value=sentinel_dir,
            ),
        ):
            count = handle_session_end()

        assert count == 1
        assert (sentinel_dir / "kai-abc").exists()

    def test_malformed_active_file_skipped(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "bad").write_text("only-one-line\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=fake_root),
            patch("biff.config.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0


# ── handle_post_checkout ───────────────────────────────────────────


class TestHandlePostCheckout:
    """Git post-checkout handler — plan hint file writing."""

    def test_branch_checkout_writes_hint(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch("biff.hook._get_git_branch", return_value="feature/auth"),
            home_mock,
            wt_mock,
        ):
            result = handle_post_checkout("1")

        assert result is not None
        assert "→ feature/auth" in result
        hint = _hint_path(tmp_path, "plan-hint").read_text().strip()
        assert "→ feature/auth" in hint

    def test_file_checkout_ignored(self) -> None:
        assert handle_post_checkout("0") is None

    def test_empty_branch_flag_ignored(self) -> None:
        assert handle_post_checkout("") is None

    def test_bead_branch_expanded(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch("biff.hook._get_git_branch", return_value="jmf/biff-ka4"),
            patch(
                "biff.server.tools.plan.expand_bead_id",
                return_value="biff-ka4: post-checkout hook",
            ),
            home_mock,
            wt_mock,
        ):
            result = handle_post_checkout("1")

        assert result is not None
        assert "biff-ka4: post-checkout hook" in result

    def test_main_branch_writes_empty_hint(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch("biff.hook._get_git_branch", return_value="main"),
            home_mock,
            wt_mock,
        ):
            result = handle_post_checkout("1")

        assert result is None  # Empty hint returns None
        hint = _hint_path(tmp_path, "plan-hint").read_text().strip()
        assert hint == ""

    def test_master_branch_writes_empty_hint(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch("biff.hook._get_git_branch", return_value="master"),
            home_mock,
            wt_mock,
        ):
            result = handle_post_checkout("1")

        assert result is None

    def test_no_branch_returns_none(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            assert handle_post_checkout("1") is None


# ── check_plan_hint ────────────────────────────────────────────────


class TestCheckPlanHint:
    """Plan hint file reading and cleanup."""

    def test_reads_and_deletes_hint(self, tmp_path: Path) -> None:
        hp = _hint_path(tmp_path, "plan-hint")
        hp.parent.mkdir(parents=True)
        hp.write_text("→ feature/auth\n")

        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = check_plan_hint()

        assert result is not None
        assert "→ feature/auth" in result
        assert 'source="auto"' in result
        assert not hp.exists()

    def test_empty_hint_clears_plan(self, tmp_path: Path) -> None:
        hp = _hint_path(tmp_path, "plan-hint")
        hp.parent.mkdir(parents=True)
        hp.write_text("\n")

        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = check_plan_hint()

        assert result is not None
        assert "default branch" in result
        assert 'message=""' in result

    def test_no_hint_returns_none(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            assert check_plan_hint() is None

    def test_bead_expanded_hint(self, tmp_path: Path) -> None:
        hp = _hint_path(tmp_path, "plan-hint")
        hp.parent.mkdir(parents=True)
        hp.write_text("→ biff-ka4: post-checkout hook\n")

        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = check_plan_hint()

        assert result is not None
        assert "biff-ka4: post-checkout hook" in result

    def test_quotes_in_content_escaped(self, tmp_path: Path) -> None:
        hp = _hint_path(tmp_path, "plan-hint")
        hp.parent.mkdir(parents=True)
        hp.write_text('→ feature/"quoted"\n')

        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = check_plan_hint()

        assert result is not None
        # Escaped quotes don't break the message="..." syntax
        assert r"feature/\"quoted\"" in result
        assert 'source="auto"' in result


# ── handle_post_commit ─────────────────────────────────────────────


class TestHandlePostCommit:
    """Git post-commit handler — plan hint with commit subject."""

    def test_writes_hint_with_checkmark(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch(
                "biff.hook._get_commit_subject",
                return_value="feat: auto-assign TTY",
            ),
            home_mock,
            wt_mock,
        ):
            result = handle_post_commit()

        assert result == "✓ feat: auto-assign TTY"
        hint = _hint_path(tmp_path, "plan-hint").read_text().strip()
        assert hint == "✓ feat: auto-assign TTY"

    def test_empty_subject_returns_none(self) -> None:
        with patch("biff.hook._get_commit_subject", return_value=""):
            assert handle_post_commit() is None

    def test_hint_picked_up_by_check(self, tmp_path: Path) -> None:
        """End-to-end: post-commit writes hint, check_plan_hint reads it."""
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch(
                "biff.hook._get_commit_subject",
                return_value="fix: status bar height",
            ),
            home_mock,
            wt_mock,
        ):
            handle_post_commit()

        home_mock2, wt_mock2 = _hint_mocks(tmp_path)
        with home_mock2, wt_mock2:
            result = check_plan_hint()

        assert result is not None
        assert "✓ fix: status bar height" in result
        assert 'source="auto"' in result


# ── handle_pre_push ────────────────────────────────────────────────


class TestHandlePrePush:
    """Git pre-push handler — wall hint for default branch pushes."""

    def test_main_branch_writes_hint(self, tmp_path: Path) -> None:
        lines = ["abc123 def456 refs/heads/main 000000"]
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = handle_pre_push(lines)

        assert result is not None
        assert "default branch" in result
        assert _hint_path(tmp_path, "wall-hint").exists()

    def test_master_branch_writes_hint(self, tmp_path: Path) -> None:
        lines = ["abc123 def456 refs/heads/master 000000"]
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = handle_pre_push(lines)

        assert result is not None

    def test_feature_branch_ignored(self) -> None:
        lines = ["abc123 def456 refs/heads/feature/auth 000000"]
        assert handle_pre_push(lines) is None

    def test_empty_refs_ignored(self) -> None:
        assert handle_pre_push([]) is None

    def test_multiple_refs_detects_main(self, tmp_path: Path) -> None:
        lines = [
            "abc123 def456 refs/heads/feature/auth 000000",
            "abc123 def456 refs/heads/main 000000",
        ]
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = handle_pre_push(lines)

        assert result is not None


# ── check_wall_hint ────────────────────────────────────────────────


class TestCheckWallHint:
    """Wall hint file reading and cleanup."""

    def test_reads_and_deletes_hint(self, tmp_path: Path) -> None:
        hp = _hint_path(tmp_path, "wall-hint")
        hp.parent.mkdir(parents=True)
        hp.write_text("Pushed to default branch\n")

        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            result = check_wall_hint()

        assert result is not None
        assert "/wall" in result
        assert not hp.exists()

    def test_no_hint_returns_none(self, tmp_path: Path) -> None:
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            assert check_wall_hint() is None

    def test_end_to_end(self, tmp_path: Path) -> None:
        """Pre-push writes hint, check_wall_hint reads it."""
        lines = ["abc123 def456 refs/heads/main 000000"]
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with home_mock, wt_mock:
            handle_pre_push(lines)

        home_mock2, wt_mock2 = _hint_mocks(tmp_path)
        with home_mock2, wt_mock2:
            result = check_wall_hint()

        assert result is not None
        assert "/wall" in result


# ── Worktree isolation ────────────────────────────────────────────


class TestWorktreeIsolation:
    """Hint files are scoped by worktree — no cross-session races."""

    def test_different_worktrees_isolated(self, tmp_path: Path) -> None:
        """Hint written in worktree A is invisible from worktree B."""
        wt_a = "/repo/worktree-a"
        wt_b = "/repo/worktree-b"
        hash_a = hashlib.sha256(wt_a.encode()).hexdigest()[:16]

        # Write hint in worktree A
        hp = tmp_path / ".biff" / "hints" / hash_a / "plan-hint"
        hp.parent.mkdir(parents=True)
        hp.write_text("→ feature/auth\n")

        # Read from worktree B — should see nothing
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.hook._get_worktree_root", return_value=wt_b),
        ):
            result = check_plan_hint()

        assert result is None
        assert hp.exists()  # Not consumed by B

    def test_same_worktree_shares_hints(self, tmp_path: Path) -> None:
        """Two sessions in the same worktree share hints (by design)."""
        home_mock, wt_mock = _hint_mocks(tmp_path)
        with (
            patch("biff.hook._get_git_branch", return_value="feature/auth"),
            home_mock,
            wt_mock,
        ):
            handle_post_checkout("1")

        home_mock2, wt_mock2 = _hint_mocks(tmp_path)
        with home_mock2, wt_mock2:
            result = check_plan_hint()

        assert result is not None
        assert "feature/auth" in result


# ── _detect_collisions ─────────────────────────────────────────────


def _collision_mocks(
    tmp_path: Path,
    *,
    worktree: str = _FAKE_WORKTREE,
    repo_root: Path | None = None,
    repo_slug: str | None = None,
):
    """Build mock context managers for collision detection tests."""
    fake_root = repo_root or tmp_path / "my-repo"
    return (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("biff.hook._get_worktree_root", return_value=worktree),
        patch("biff.config.find_git_root", return_value=fake_root),
        patch("biff.config.get_repo_slug", return_value=repo_slug),
    )


class TestDetectCollisions:
    """Collision detection for concurrent sessions in the same worktree."""

    def test_no_active_dir(self, tmp_path: Path) -> None:
        """No ~/.biff/active/ directory → empty list."""
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_empty_active_dir(self, tmp_path: Path) -> None:
        """Empty active directory → empty list."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_different_repo_ignored(self, tmp_path: Path) -> None:
        """Active session in a different repo → not a collision."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text(f"kai:abc\nother-repo\n{_FAKE_WORKTREE}\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_different_worktree_ignored(self, tmp_path: Path) -> None:
        """Same repo but different worktree → not a collision."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text("kai:abc\nmy-repo\n/other/worktree\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_same_repo_same_worktree_detected(self, tmp_path: Path) -> None:
        """Same repo AND same worktree → collision."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text(f"kai:abc\nmy-repo\n{_FAKE_WORKTREE}\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            result = _detect_collisions()
        assert result == ["kai:abc"]

    def test_old_format_no_worktree_conservative(self, tmp_path: Path) -> None:
        """Old 2-line format (no worktree) → treated as collision."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text("kai:abc\nmy-repo\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            result = _detect_collisions()
        assert result == ["kai:abc"]

    def test_multiple_collisions(self, tmp_path: Path) -> None:
        """Multiple sessions in same repo+worktree → all returned."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text(f"kai:abc\nmy-repo\n{_FAKE_WORKTREE}\n")
        (active_dir / "eric-def").write_text(f"eric:def\nmy-repo\n{_FAKE_WORKTREE}\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            result = _detect_collisions()
        assert sorted(result) == ["eric:def", "kai:abc"]

    def test_no_git_root(self, tmp_path: Path) -> None:
        """No git root → empty list."""
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.config.find_git_root", return_value=None),
        ):
            assert _detect_collisions() == []

    def test_session_start_includes_advisory(self, tmp_path: Path) -> None:
        """handle_session_start() includes collision advisory."""
        active_dir = tmp_path / ".biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text(f"kai:abc\nmy-repo\n{_FAKE_WORKTREE}\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with (
            patch("biff.hook._get_git_branch", return_value="main"),
            m_home,
            m_wt,
            m_git,
            m_slug,
        ):
            result = handle_session_start({})
        assert "\u26a0" in result
        assert "kai:abc" in result
        assert "/who" in result
        assert "worktree" in result

    def test_session_start_no_collision_no_advisory(self, tmp_path: Path) -> None:
        """handle_session_start() without collisions has no advisory."""
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with (
            patch("biff.hook._get_git_branch", return_value="main"),
            m_home,
            m_wt,
            m_git,
            m_slug,
        ):
            result = handle_session_start({})
        assert "\u26a0" not in result
