"""Tests for biff hook dispatcher (DES-017).

Unit tests for the pure handler functions.  These test business logic
without I/O — no stdin/stdout mocking, no git repo, no config files.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import cast
from unittest.mock import patch

from biff.hook import (
    _detect_collisions,
    _expand_branch_plan,
    _has_active_session,
    _read_hook_input,
    cc_pre_tool_use,
    check_plan_hint,
    check_wall_hint,
    handle_post_bash,
    handle_post_checkout,
    handle_post_commit,
    handle_post_pr,
    handle_pre_compact,
    handle_pre_push,
    handle_pre_tool_use,
    handle_session_end,
    handle_session_resume,
    handle_session_start,
)

# Deterministic worktree root for hint file tests.
_FAKE_WORKTREE = "/test/worktree"
_FAKE_HINT_HASH = hashlib.sha256(_FAKE_WORKTREE.encode()).hexdigest()[:16]


def _hint_mocks(tmp_path: Path):
    """Context manager stack mocking Path.home and repo-common-root."""
    return (
        patch("pathlib.Path.home", return_value=tmp_path),
        patch("biff.hook._repo_common_root", return_value=_FAKE_WORKTREE),
    )


def _hint_path(tmp_path: Path, name: str) -> Path:
    """Expected hint file path for tests."""
    return tmp_path / ".punt-labs" / "biff" / "hints" / _FAKE_HINT_HASH / name


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

    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_create_pr_github_prefix(self, _mock: object) -> None:
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
        assert '" 10m' in result

    @patch("biff.hook._is_lux_enabled", return_value=False)
    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_create_pr_plugin_prefix(self, _wall: object, _lux: object) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__plugin_github_github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": json.dumps({"number": 42}),
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Created PR #42" in result

    @patch("biff.hook._is_lux_enabled", return_value=False)
    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_create_pr_response_as_dict(self, _wall: object, _lux: object) -> None:
        """tool_response may arrive as dict instead of JSON string."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hook dispatcher"},
            "tool_response": {"number": 42},
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Created PR #42" in result

    @patch("biff.hook._is_lux_enabled", return_value=False)
    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_merge_pr_with_title(self, _wall: object, _lux: object) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pullNumber": 42, "commit_title": "feat: hook dispatcher"},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Merged PR #42" in result
        assert "feat: hook dispatcher" in result

    @patch("biff.hook._is_lux_enabled", return_value=False)
    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_merge_pr_pull_number_field(self, _wall: object, _lux: object) -> None:
        """Both pullNumber and pull_number field names are accepted."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pull_number": 42},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert "Merged PR #42" in result

    @patch("biff.markers.read_wall_marker", return_value=None)
    def test_merge_pr_no_title(self, _mock: object) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pullNumber": 42},
            "tool_response": "{}",
        }
        result = handle_post_pr(data)
        assert result is not None
        assert '/wall "Merged PR #42"' in result
        # No title means the msg is "Merged PR #42" with no ": title" suffix
        assert "Merged PR #42:" not in result

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

    def test_wall_active_skips_wall_suggestion(self) -> None:
        """When a wall is already active, skip /wall but keep Lux if enabled."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hooks"},
            "tool_response": json.dumps({"number": 99}),
        }
        with (
            patch("biff.markers.read_wall_marker", return_value="deploy freeze"),
            patch("biff.hook._is_lux_enabled", return_value=False),
        ):
            result = handle_post_pr(data)
        assert result is None

    def test_wall_active_with_lux_shows_dashboard(self) -> None:
        """Wall active + Lux enabled: skip /wall, still show Lux dashboard."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hooks"},
            "tool_response": json.dumps({"number": 99}),
        }
        with (
            patch("biff.markers.read_wall_marker", return_value="deploy freeze"),
            patch("biff.hook._is_lux_enabled", return_value=True),
        ):
            result = handle_post_pr(data)
        assert result is not None
        assert "/wall" not in result
        assert "/lux:dashboard" in result

    def test_no_wall_includes_wall_suggestion(self) -> None:
        """When no wall is active, suggest /wall with 10m TTL."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hooks"},
            "tool_response": json.dumps({"number": 99}),
        }
        with patch("biff.markers.read_wall_marker", return_value=None):
            result = handle_post_pr(data)
        assert result is not None
        assert "/wall" in result
        assert '" 10m' in result


# ── handle_session_start ────────────────────────────────────────────


class TestHandleSessionStart:
    """SessionStart(startup) handler — branch detection and nudges."""

    def test_includes_tty_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start()
        assert "/tty" in result

    def test_includes_read_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start()
        assert "/read" in result

    def test_branch_included_in_plan_nudge(self) -> None:
        with patch("biff.hook._get_git_branch", return_value="feature/auth"):
            result = handle_session_start()
        assert "→ feature/auth" in result
        assert 'source="auto"' in result

    def test_bead_branch_expanded(self) -> None:
        with (
            patch("biff.hook._get_git_branch", return_value="jmf/biff-ka4"),
            patch(
                "biff._stdlib.expand_bead_id",
                return_value="biff-ka4: post-checkout hook",
            ),
        ):
            result = handle_session_start()
        assert "→ biff-ka4: post-checkout hook" in result

    def test_no_branch_still_returns_context(self) -> None:
        with patch("biff.hook._get_git_branch", return_value=""):
            result = handle_session_start()
        assert "Biff session starting" in result
        assert "/plan" in result

    def test_main_branch_included(self) -> None:
        with patch("biff.hook._get_git_branch", return_value="main"):
            result = handle_session_start()
        assert "→ main" in result

    def test_quotes_in_branch_escaped(self) -> None:
        with patch("biff.hook._get_git_branch", return_value='feature/"quotes"'):
            result = handle_session_start()
        assert r"\"quotes\"" in result
        assert 'source="auto"' in result

    def test_active_wall_included(self, tmp_path: Path) -> None:
        """Active (non-expired) wall text appears in startup context."""
        from datetime import UTC, datetime, timedelta

        from biff.markers import write_wall_marker

        future = datetime.now(UTC) + timedelta(hours=1)
        m_home, m_wt = _hint_mocks(tmp_path)
        with (
            m_home,
            m_wt,
            patch("biff.hook._get_git_branch", return_value="main"),
        ):
            write_wall_marker(_FAKE_WORKTREE, "deploy freeze", future)
            result = handle_session_start()
        assert "Active wall: deploy freeze" in result

    def test_no_wall_no_wall_line(self, tmp_path: Path) -> None:
        """Without a wall marker, no wall text in startup context."""
        m_home, m_wt = _hint_mocks(tmp_path)
        with (
            m_home,
            m_wt,
            patch("biff.hook._get_git_branch", return_value="main"),
        ):
            result = handle_session_start()
        assert "Active wall" not in result


# ── handle_session_resume ───────────────────────────────────────────


class TestHandleSessionResume:
    """SessionStart(resume|compact) handler — re-orientation nudge."""

    def test_includes_read_nudge(self) -> None:
        result = handle_session_resume()
        assert "/read" in result

    def test_mentions_resume(self) -> None:
        result = handle_session_resume()
        assert "resumed" in result


class TestCaptureSessionHint:
    """SessionStart persists the Claude session_id for the server (biff-7ak)."""

    def test_writes_hint_from_payload(self, tmp_path: Path) -> None:
        import biff.session_id as sid_mod
        from biff.hook import _capture_session_hint

        with (
            patch.object(sid_mod, "biff_data_dir", return_value=tmp_path),
            patch.object(sid_mod, "_resolve_claude_pid", return_value=4321),
            patch.object(sid_mod, "_process_start_time", return_value=42.0),
        ):
            _capture_session_hint({"session_id": "sid-from-hook", "source": "resume"})
            loaded = sid_mod.SessionHint.load(4321)

        assert loaded is not None
        assert loaded.session_id == "sid-from-hook"
        assert loaded.source == "resume"
        assert loaded.claude_pid == 4321

    def test_no_session_id_is_noop(self, tmp_path: Path) -> None:
        import biff.session_id as sid_mod
        from biff.hook import _capture_session_hint

        with patch.object(sid_mod, "biff_data_dir", return_value=tmp_path):
            _capture_session_hint({"source": "startup"})

        assert not (tmp_path / "sessions").exists()

    def test_write_failure_is_swallowed(self, tmp_path: Path) -> None:
        """A hint-write OSError must not break session startup (best-effort)."""
        import biff.session_id as sid_mod
        from biff.hook import _capture_session_hint

        def _boom(_self: object) -> None:
            raise OSError("disk full")

        with (
            patch.object(sid_mod, "biff_data_dir", return_value=tmp_path),
            patch.object(sid_mod, "_resolve_claude_pid", return_value=4321),
            patch.object(sid_mod, "_process_start_time", return_value=42.0),
            patch.object(sid_mod.SessionHint, "write", _boom),
        ):
            # Must not raise.
            _capture_session_hint({"session_id": "sid", "source": "startup"})


# ── handle_pre_compact ─────────────────────────────────────────────


class TestHandlePreCompact:
    """PreCompact handler — plan injection before context compaction."""

    def test_with_plan_marker(self, tmp_path: Path) -> None:
        """When plan marker exists, plan text is injected."""
        from biff.markers import write_plan_marker

        m_home, m_wt = _hint_mocks(tmp_path)
        with m_home, m_wt:
            write_plan_marker(_FAKE_WORKTREE, "sid-1", "biff-sgl: PreCompact hook")
            result = handle_pre_compact({"session_id": "sid-1"})
        assert "Current biff plan: biff-sgl: PreCompact hook" in result
        assert "/read" in result

    def test_without_plan_marker(self) -> None:
        """When no plan marker, returns generic compaction message."""
        with patch("biff.hook._repo_common_root", return_value=""):
            result = handle_pre_compact({"session_id": "sid-1"})
        assert "resumed after compaction" in result
        assert "/read" in result

    def test_no_data_returns_generic_message(self) -> None:
        """Called with no payload at all (defensive default) still works."""
        with patch("biff.hook._repo_common_root", return_value=""):
            result = handle_pre_compact()
        assert "resumed after compaction" in result

    def test_different_identity_sees_no_plan(self, tmp_path: Path) -> None:
        """PreCompact reads only the CALLING session's own plan marker."""
        from biff.markers import write_plan_marker

        m_home, m_wt = _hint_mocks(tmp_path)
        with m_home, m_wt:
            write_plan_marker(_FAKE_WORKTREE, "sid-1", "sid-1's plan")
            result = handle_pre_compact({"session_id": "sid-2"})
        assert "resumed after compaction" in result

    def test_no_git_root(self) -> None:
        """When not in a git repo, returns generic compaction message."""
        with (
            patch("biff._stdlib.find_git_root", return_value=None),
            patch("biff.markers.read_plan_marker", return_value=None),
        ):
            result = handle_pre_compact()
        assert "resumed after compaction" in result


# ── _expand_branch_plan ─────────────────────────────────────────────


class TestExpandBranchPlan:
    """Branch name to plan string conversion."""

    def test_plain_branch(self) -> None:
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            assert _expand_branch_plan("feature/auth") == "→ feature/auth"

    def test_bead_id_in_branch(self) -> None:
        with patch(
            "biff._stdlib.expand_bead_id",
            return_value="biff-ka4: post-checkout hook",
        ):
            result = _expand_branch_plan("jmf/biff-ka4")
        assert result == "→ biff-ka4: post-checkout hook"

    def test_bead_id_at_start(self) -> None:
        with patch(
            "biff._stdlib.expand_bead_id",
            return_value="biff-ka4: hook",
        ):
            result = _expand_branch_plan("biff-ka4-description")
        assert result == "→ biff-ka4: hook"

    def test_no_bead_id(self) -> None:
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            assert _expand_branch_plan("main") == "→ main"

    def test_expansion_failure_uses_raw_branch(self) -> None:
        """If expand_bead_id returns the ID unchanged, use the full branch."""
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("jmf/biff-xyz")
        assert result == "→ biff-xyz"

    def test_no_false_positive_on_common_branch(self) -> None:
        """Branch names like my-feature must not be truncated to my-feat."""
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("my-feature")
        assert result == "→ my-feature"

    def test_no_false_positive_fix_tests(self) -> None:
        """fix-tests must not match as a bead ID."""
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("fix-tests")
        assert result == "→ fix-tests"

    def test_no_false_positive_add_logging(self) -> None:
        with patch(
            "biff._stdlib.expand_bead_id",
            side_effect=_identity,
        ):
            result = _expand_branch_plan("add-logging")
        assert result == "→ add-logging"


# ── handle_session_end ──────────────────────────────────────────────


class TestHandleSessionEnd:
    """SessionEnd handler — active-to-sentinel conversion."""

    def test_converts_active_to_sentinel(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc12345").write_text("kai:abc12345\nmy-repo\n")

        sentinel_dir = tmp_path / "sentinels" / "my-repo"
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
            patch(
                "biff._stdlib.sentinel_dir",
                return_value=sentinel_dir,
            ),
        ):
            count = handle_session_end()

        assert count == 1
        assert (sentinel_dir / "kai-abc12345").read_text() == "kai:abc12345"
        assert not (active_dir / "kai-abc12345").exists()

    def test_empty_active_dir(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0

    def test_no_active_dir(self, tmp_path: Path) -> None:
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0

    def test_scoped_to_current_repo(self, tmp_path: Path) -> None:
        """Only cleans up sessions matching the current repo name."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-aaa").write_text("kai:aaa\nmy-repo\n")
        (active_dir / "kai-bbb").write_text("kai:bbb\nother-repo\n")

        sentinel_dir = tmp_path / "sentinels" / "my-repo"
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
            patch(
                "biff._stdlib.sentinel_dir",
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
        with patch("biff._stdlib.find_git_root", return_value=None):
            count = handle_session_end()

        assert count == 0

    def test_matches_sanitized_repo_slug(self, tmp_path: Path) -> None:
        """Active file uses sanitized slug (owner__repo), not directory name."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text("kai:abc\npunt-labs__biff\n")

        sentinel_dir = tmp_path / "sentinels" / "punt-labs__biff"
        fake_root = tmp_path / "biff"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch(
                "biff._stdlib.get_repo_slug",
                return_value="punt-labs/biff",
            ),
            patch(
                "biff._stdlib.sentinel_dir",
                return_value=sentinel_dir,
            ),
        ):
            count = handle_session_end()

        assert count == 1
        assert (sentinel_dir / "kai-abc").exists()

    def test_malformed_active_file_skipped(self, tmp_path: Path) -> None:
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "bad").write_text("only-one-line\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
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
                "biff._stdlib.expand_bead_id",
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
        assert '" 10m' in result
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
        hp = tmp_path / ".punt-labs" / "biff" / "hints" / hash_a / "plan-hint"
        hp.parent.mkdir(parents=True)
        hp.write_text("→ feature/auth\n")

        # Read from worktree B — should see nothing
        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff.hook._repo_common_root", return_value=wt_b),
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
        patch("biff.hook._repo_common_root", return_value=worktree),
        patch("biff._stdlib.find_git_root", return_value=fake_root),
        patch("biff._stdlib.get_repo_slug", return_value=repo_slug),
    )


class TestDetectCollisions:
    """Collision detection for concurrent sessions in the same worktree."""

    def test_no_active_dir(self, tmp_path: Path) -> None:
        """No ~/.punt-labs/biff/active/ directory → empty list."""
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_empty_active_dir(self, tmp_path: Path) -> None:
        """Empty active directory → empty list."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_different_repo_ignored(self, tmp_path: Path) -> None:
        """Active session in a different repo → not a collision."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text(f"kai:abc\nother-repo\n{_FAKE_WORKTREE}\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_different_worktree_ignored(self, tmp_path: Path) -> None:
        """Same repo but different worktree → not a collision."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc").write_text("kai:abc\nmy-repo\n/other/worktree\n")
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()
        m_home, m_wt, m_git, m_slug = _collision_mocks(tmp_path, repo_root=fake_root)
        with m_home, m_wt, m_git, m_slug:
            assert _detect_collisions() == []

    def test_same_repo_same_worktree_detected(self, tmp_path: Path) -> None:
        """Same repo AND same worktree → collision."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
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
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
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
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
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
            patch("biff._stdlib.find_git_root", return_value=None),
        ):
            assert _detect_collisions() == []

    def test_session_start_includes_advisory(self, tmp_path: Path) -> None:
        """Collision advisory includes coordination guidance."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
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
            result = handle_session_start()
        assert "\u26a0" in result
        assert "kai:abc" in result
        assert "/who" in result
        assert "/plan" in result
        assert "/write" in result
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
            result = handle_session_start()
        assert "\u26a0" not in result


# ── handle_pre_tool_use ──────────────────────────────────────────────


def _gate_mocks(*, plan: bool):
    """Return patches for the PreToolUse plan gate.

    Patches ``_has_active_session`` to ``True`` so the gate logic is
    exercised (the liveness check is tested separately).
    """
    return (
        patch("biff.hook._has_active_session", return_value=True),
        patch("biff.hook._repo_common_root", return_value=_FAKE_WORKTREE),
        patch("biff.markers.has_plan_marker", return_value=plan),
    )


def _deny_reason(result: dict[str, object]) -> str:
    """Extract the deny reason from a PreToolUse hard-gate response.

    The gate is a hard deny (DES-051): ``permissionDecision`` must be
    ``"deny"`` and the actionable instructions travel in
    ``permissionDecisionReason`` (DES-026), never a non-blocking
    ``additionalContext`` nudge.
    """
    output = cast("dict[str, object]", result["hookSpecificOutput"])
    assert output["hookEventName"] == "PreToolUse"
    assert output["permissionDecision"] == "deny", (
        "PreToolUse gate must hard-deny, not nudge via additionalContext"
    )
    assert "additionalContext" not in output, (
        "deny response carries its reason in permissionDecisionReason"
    )
    return str(output["permissionDecisionReason"])


class TestHasActiveSession:
    """Liveness check: _has_active_session() and PreToolUse early return."""

    def test_no_active_dir(self, tmp_path: Path) -> None:
        """Missing active directory returns False."""
        with patch("biff._stdlib.biff_data_dir", return_value=tmp_path):
            assert _has_active_session() is False

    def test_empty_active_dir(self, tmp_path: Path) -> None:
        """Empty active directory returns False."""
        (tmp_path / "active").mkdir()
        with patch("biff._stdlib.biff_data_dir", return_value=tmp_path):
            assert _has_active_session() is False

    def test_active_session_file_returns_true(self, tmp_path: Path) -> None:
        """Active directory with a session file returns True."""
        active = tmp_path / "active"
        active.mkdir()
        (active / "kai-abc12345").write_text("kai:abc12345\nmy-repo\n")
        with patch("biff._stdlib.biff_data_dir", return_value=tmp_path):
            assert _has_active_session() is True

    def test_pre_tool_use_returns_none_without_active_session(self) -> None:
        """handle_pre_tool_use returns None when no MCP server is running."""
        with patch("biff.hook._has_active_session", return_value=False):
            result = handle_pre_tool_use({})
        assert result is None

    def test_pre_tool_use_still_gates_with_active_session(self) -> None:
        """handle_pre_tool_use hard-denies when server IS running and plan not set."""
        m_active, m_wt, m_plan = _gate_mocks(plan=False)
        with m_active, m_wt, m_plan:
            result = handle_pre_tool_use({})
        assert result is not None
        reason = _deny_reason(result)
        assert "/plan" in reason


class TestHandlePreToolUse:
    """PreToolUse gate: hard-deny Edit/Write without a set plan.

    Conforms to the ``claude-code-biff.tex`` Z model: ``PreToolHookAllow``
    is enabled for edit tools only when ``planSet = ztrue``; otherwise
    only ``PreToolHookDeny`` (``pdDeny``) is reachable.  The gate depends
    on the plan marker alone — never on beads or ``bd`` (DES-051).
    """

    def test_plan_missing_denies_with_plan_instruction(self) -> None:
        m_active, m_wt, m_plan = _gate_mocks(plan=False)
        with m_active, m_wt, m_plan:
            result = handle_pre_tool_use({})
        assert result is not None
        reason = _deny_reason(result)
        assert "/plan" in reason
        assert "bd" not in reason

    def test_plan_present_allows(self) -> None:
        m_active, m_wt, m_plan = _gate_mocks(plan=True)
        with m_active, m_wt, m_plan:
            result = handle_pre_tool_use({})
        assert result is None

    def test_gate_never_invokes_bd(self) -> None:
        """The gate path must not shell out to ``bd`` (biff-84a)."""
        m_active, m_wt, m_plan = _gate_mocks(plan=False)
        with (
            m_active,
            m_wt,
            m_plan,
            patch("subprocess.run") as mock_run,
        ):
            handle_pre_tool_use({})
        mock_run.assert_not_called()


class TestPreToolUseFailsClosed:
    """The gate entrypoint denies when it cannot evaluate its condition.

    A hard control that silently grants access on an unexpected error is
    the same bug as no control at all (DES-051). If reading the plan
    marker raises, ``cc_pre_tool_use`` must emit a deny, not let the edit
    through.
    """

    def test_marker_read_error_emits_deny(self) -> None:
        emitted: list[dict[str, object]] = []
        with (
            patch("biff.hook._is_biff_enabled", return_value=True),
            patch("biff.hook._read_hook_input", return_value={}),
            patch("biff.hook._has_active_session", return_value=True),
            patch("biff.hook._repo_common_root", return_value=_FAKE_WORKTREE),
            patch("biff.markers.has_plan_marker", side_effect=OSError("boom")),
            patch("biff.hook._emit", side_effect=emitted.append),
        ):
            cc_pre_tool_use()
        assert len(emitted) == 1
        reason = _deny_reason(emitted[0])
        assert "/plan" in reason
        assert "bd" not in reason


# ── Z spec invariant coverage (biff-g9b) ─────────────────────────────


class TestZSpecPlanConsistency:
    """Z spec §4 invariant 14: planSet=zfalse => planSource=empty.

    In the implementation, UserSession always has plan_source="manual"
    as a default. The Z spec invariant maps to: when plan is empty,
    the plan_source value is irrelevant (default state). We verify
    the session model's default state satisfies this.
    """

    def test_new_session_has_empty_plan(self) -> None:
        """A fresh UserSession has plan="" (planSet = zfalse)."""
        from biff.models import UserSession

        session = UserSession(user="kai")
        assert session.plan == ""

    def test_plan_set_has_source(self) -> None:
        """When plan is set, plan_source must be present (invariant 15)."""
        from biff.models import UserSession

        session = UserSession(user="kai", plan="working on hooks")
        assert session.plan_source in ("manual", "auto")

    def test_session_start_clears_plan_marker(self, tmp_path: Path) -> None:
        """SessionStart clears its own stale marker (planSet'(identity) = zfalse)."""
        from biff.markers import has_plan_marker, write_plan_marker

        m_home, m_wt = _hint_mocks(tmp_path)
        with m_home, m_wt:
            write_plan_marker(_FAKE_WORKTREE, "sid-1", "stale plan")
            assert has_plan_marker(_FAKE_WORKTREE, "sid-1")
            with patch("biff.hook._get_git_branch", return_value="main"):
                handle_session_start({"session_id": "sid-1"})
            assert not has_plan_marker(_FAKE_WORKTREE, "sid-1")

    def test_session_start_does_not_clear_sibling_identity(
        self, tmp_path: Path
    ) -> None:
        """A concurrent sibling session's marker survives another's SessionStart.

        This is the direct fix for om9's core mechanism: before scoping the
        marker by identity, every ``SessionStart`` unconditionally cleared
        the ONE shared marker file, revoking a plan another session's gate
        was already relying on.
        """
        from biff.markers import has_plan_marker, write_plan_marker

        m_home, m_wt = _hint_mocks(tmp_path)
        with m_home, m_wt:
            write_plan_marker(_FAKE_WORKTREE, "sid-sibling", "sibling's plan")
            with patch("biff.hook._get_git_branch", return_value="main"):
                handle_session_start({"session_id": "sid-new"})
            assert has_plan_marker(_FAKE_WORKTREE, "sid-sibling")


class TestZSpecSessionEndCleanup:
    """Z spec §10 invariants 22-25: spInactive => biff state cleared.

    Session end cleanup happens in two layers:
    1. handle_session_end() (hook) — active marker → sentinel
    2. MCP server lifespan — relay session, unread file, active marker

    These tests verify the hook layer. The lifespan layer is tested
    in the integration test suite.
    """

    def test_session_end_removes_active_marker(self, tmp_path: Path) -> None:
        """Invariant 22-25: active session file is removed at end."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        (active_dir / "kai-abc12345").write_text("kai:abc12345\nmy-repo\n")

        sentinel_dir = tmp_path / "sentinels" / "my-repo"
        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
            patch("biff._stdlib.sentinel_dir", return_value=sentinel_dir),
        ):
            count = handle_session_end()

        assert count == 1
        # Active marker gone — session no longer visible to collision detection
        assert not (active_dir / "kai-abc12345").exists()
        # Sentinel exists for reaper to clean up relay state
        assert (sentinel_dir / "kai-abc12345").exists()

    def test_session_end_does_not_touch_other_repos(self, tmp_path: Path) -> None:
        """Only this repo's sessions are cleaned — invariant scoped per-repo."""
        active_dir = tmp_path / ".punt-labs" / "biff" / "active"
        active_dir.mkdir(parents=True)
        # Session for a different repo
        (active_dir / "kai-xyz99999").write_text("kai:xyz99999\nother-repo\n")

        fake_root = tmp_path / "my-repo"
        fake_root.mkdir()

        with (
            patch("pathlib.Path.home", return_value=tmp_path),
            patch("biff._stdlib.find_git_root", return_value=fake_root),
            patch("biff._stdlib.get_repo_slug", return_value=None),
        ):
            count = handle_session_end()

        assert count == 0
        # Other repo's session untouched
        assert (active_dir / "kai-xyz99999").exists()


# ── Lux consumer hooks (biff-og4p, biff-g75a) ─────────────────────────


def _lux_mocks(*, beads: bool = True, lux: bool = True):
    """Patch beads + lux detection for consumer hook tests."""
    return (
        patch("biff.hook._has_beads", return_value=beads),
        patch("biff.hook._is_lux_enabled", return_value=lux),
    )


class TestLuxBeadsBoardRefresh:
    """biff-og4p: refresh lux beads board on bd state changes."""

    def test_bd_create_with_lux_nudges_refresh(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title='Fix bug'"},
            "tool_response": "\u2713 Created issue: biff-xyz",
        }
        m_beads, m_lux = _lux_mocks()
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is not None
        assert "/lux:beads" in result

    def test_bd_close_with_lux_nudges_refresh(self, tmp_path: Path) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd close biff-abc"},
            "tool_response": "\u2713 Closed biff-abc",
        }
        m_beads, m_lux = _lux_mocks()
        m_home, m_wt = _hint_mocks(tmp_path)
        with m_beads, m_lux, m_home, m_wt:
            result = handle_post_bash(data)
        assert result is not None
        assert "/lux:beads" in result

    def test_bd_update_status_with_lux_nudges_refresh(self, tmp_path: Path) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd update biff-xyz --status=in_progress"},
            "tool_response": "\u2713 Updated issue: biff-xyz",
        }
        m_beads, m_lux = _lux_mocks()
        m_home, m_wt = _hint_mocks(tmp_path)
        with m_beads, m_lux, m_home, m_wt:
            result = handle_post_bash(data)
        assert result is not None
        assert "/plan" in result  # claim nudge
        assert "/lux:beads" in result  # lux refresh

    def test_bd_dep_with_lux_nudges_refresh(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd dep add biff-a biff-b"},
            "tool_response": "\u2713 Dependency added",
        }
        m_beads, m_lux = _lux_mocks()
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is not None
        assert "/lux:beads" in result

    def test_no_lux_no_nudge(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title='Fix bug'"},
            "tool_response": "\u2713 Created issue: biff-xyz",
        }
        m_beads, m_lux = _lux_mocks(lux=False)
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is None

    def test_no_beads_no_nudge(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title='Fix bug'"},
            "tool_response": "\u2713 Created issue: biff-xyz",
        }
        m_beads, m_lux = _lux_mocks(beads=False)
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is None

    def test_failed_bd_command_no_nudge(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "bd create --title='Fix bug'"},
            "tool_response": "Error: failed to create",
        }
        m_beads, m_lux = _lux_mocks()
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is None

    def test_non_bd_command_no_nudge(self) -> None:
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
            "tool_response": "On branch main",
        }
        m_beads, m_lux = _lux_mocks()
        with m_beads, m_lux:
            result = handle_post_bash(data)
        assert result is None


class TestLuxPrDashboard:
    """biff-g75a: render PR dashboard in lux on PR creation."""

    def test_create_pr_with_lux_nudges_dashboard(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hooks"},
            "tool_response": json.dumps({"number": 42}),
        }
        with (
            patch("biff.markers.read_wall_marker", return_value=None),
            patch("biff.hook._is_lux_enabled", return_value=True),
        ):
            result = handle_post_pr(data)
        assert result is not None
        assert "/lux:dashboard" in result
        assert "PR #42" in result

    def test_create_pr_no_lux_no_dashboard(self) -> None:
        data: dict[str, object] = {
            "tool_name": "mcp__github__create_pull_request",
            "tool_input": {"title": "feat: hooks"},
            "tool_response": json.dumps({"number": 42}),
        }
        with (
            patch("biff.markers.read_wall_marker", return_value=None),
            patch("biff.hook._is_lux_enabled", return_value=False),
        ):
            result = handle_post_pr(data)
        assert result is not None
        assert "/lux:dashboard" not in result

    def test_merge_pr_no_dashboard(self) -> None:
        """Dashboard is only for PR creation, not merge."""
        data: dict[str, object] = {
            "tool_name": "mcp__github__merge_pull_request",
            "tool_input": {"pullNumber": 42, "commit_title": "feat: hooks"},
            "tool_response": "{}",
        }
        with (
            patch("biff.markers.read_wall_marker", return_value=None),
            patch("biff.hook._is_lux_enabled", return_value=True),
        ):
            result = handle_post_pr(data)
        assert result is not None
        assert "/lux:dashboard" not in result


class TestIsLuxEnabled:
    """Unit tests for _is_lux_enabled() YAML frontmatter parsing."""

    def test_lux_enabled(self, tmp_path: Path) -> None:
        """Standard .lux/config.md with display: "y"."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text('---\ndisplay: "y"\n---\n')
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is True

    def test_lux_disabled(self, tmp_path: Path) -> None:
        """display: "n" means lux is off."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text('---\ndisplay: "n"\n---\n')
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is False

    def test_lux_no_config_file(self, tmp_path: Path) -> None:
        """No .lux/config.md means lux is off."""
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is False

    def test_lux_no_frontmatter(self, tmp_path: Path) -> None:
        """Config file without --- frontmatter delimiters."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text('display: "y"\n')
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is False

    def test_lux_missing_closing_delimiter(self, tmp_path: Path) -> None:
        """Frontmatter with opening --- but no closing ---."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text('---\ndisplay: "y"\n')
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is False

    def test_lux_unquoted_value(self, tmp_path: Path) -> None:
        """display: y without quotes should still work."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text("---\ndisplay: y\n---\n")
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is True

    def test_lux_single_quoted_value(self, tmp_path: Path) -> None:
        """display: \'y\' with single quotes."""
        lux_dir = tmp_path / ".lux"
        lux_dir.mkdir()
        (lux_dir / "config.md").write_text("---\ndisplay: 'y'\n---\n")
        with patch("biff._stdlib.find_git_root", return_value=tmp_path):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is True

    def test_lux_no_git_root(self) -> None:
        """No git root means lux is off."""
        with patch("biff._stdlib.find_git_root", return_value=None):
            from biff.hook import _is_lux_enabled

            assert _is_lux_enabled() is False


class TestIsErrorFlag:
    """is_error flag suppresses nudges on failed commands."""

    def test_is_error_prevents_claim_nudge(self, tmp_path: Path) -> None:
        """Bash tool with is_error=True should not nudge on a bead claim."""
        data: dict[str, object] = {
            "tool_name": "Bash",
            "tool_input": {
                "command": "bd update biff-abc --status=in_progress",
            },
            "tool_response": "\u2713 Updated issue: biff-abc",
            "is_error": True,
        }
        m_home, m_wt = _hint_mocks(tmp_path)
        with m_home, m_wt:
            result = handle_post_bash(data)
        assert result is None


# ── _read_hook_input (non-blocking stdin) ───────────────────────────


class TestReadHookInput:
    """Tests for _read_hook_input non-blocking stdin reads."""

    def test_empty_stdin_returns_empty(self) -> None:
        """No data on stdin returns {} without blocking."""
        r_fd, w_fd = os.pipe()
        r = os.fdopen(r_fd, "r")
        # Close write end immediately — EOF with no data.
        os.close(w_fd)
        with patch("sys.stdin", r):
            result = _read_hook_input()
        r.close()
        assert result == {}

    def test_valid_json_parsed(self) -> None:
        """Valid JSON on stdin is parsed and returned."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"tool_name": "Edit"}\n')
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        with patch("sys.stdin", r):
            result = _read_hook_input()
        r.close()
        assert result == {"tool_name": "Edit"}

    def test_no_eof_does_not_hang(self) -> None:
        """Stdin with data but no EOF returns data without blocking.

        This is the regression test for the session resume hang:
        Claude Code pipes data but may not close the pipe.
        """
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b'{"event": "resume"}\n')
        # Do NOT close w_fd — simulates open pipe without EOF.
        r = os.fdopen(r_fd, "r")
        with patch("sys.stdin", r):
            result = _read_hook_input()
        r.close()
        os.close(w_fd)
        assert result == {"event": "resume"}

    def test_no_data_no_eof_returns_empty(self) -> None:
        """Open pipe with no data returns {} without blocking."""
        r_fd, w_fd = os.pipe()
        # Pipe open, no data written, no EOF.
        r = os.fdopen(r_fd, "r")
        with patch("sys.stdin", r):
            result = _read_hook_input()
        r.close()
        os.close(w_fd)
        assert result == {}

    def test_invalid_json_returns_empty(self) -> None:
        """Invalid JSON on stdin returns {} gracefully."""
        r_fd, w_fd = os.pipe()
        os.write(w_fd, b"not json\n")
        os.close(w_fd)
        r = os.fdopen(r_fd, "r")
        with patch("sys.stdin", r):
            result = _read_hook_input()
        r.close()
        assert result == {}
