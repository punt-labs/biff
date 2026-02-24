"""Tests for biff hook dispatcher (DES-017).

Unit tests for the pure handler functions.  These test business logic
without I/O — no stdin/stdout mocking, no git repo, no .biff files.
"""

from __future__ import annotations

import json

from biff.hook import handle_post_bash, handle_post_pr

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
