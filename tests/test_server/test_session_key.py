"""Tests for session key resolution (process tree walk)."""

from __future__ import annotations

import os
from unittest.mock import patch

import biff.session_key as session_key_mod
from biff.session_key import (
    _is_claude,
    _walk_to_topmost_claude,
    find_session_key,
)


def _make_table(
    entries: dict[int, tuple[int, str]],
) -> dict[int, tuple[int, str]]:
    """Identity helper for readability — just returns the dict."""
    return entries


class TestIsClaude:
    def test_bare_name(self) -> None:
        assert _is_claude("claude") is True

    def test_full_path(self) -> None:
        assert _is_claude("/usr/local/bin/claude") is True

    def test_macos_app_path(self) -> None:
        assert _is_claude("/Applications/Claude.app/Contents/MacOS/claude") is True

    def test_not_claude(self) -> None:
        assert _is_claude("python3") is False
        assert _is_claude("/bin/zsh") is False

    def test_partial_match(self) -> None:
        assert _is_claude("claude-helper") is False
        assert _is_claude("not-claude") is False


class TestWalkToTopmostClaude:
    """Test _walk_to_topmost_claude with mocked process tables."""

    def test_direct_parent_is_claude(self) -> None:
        """MCP server is direct child of claude (original DES-011 model)."""
        my_pid = os.getpid()
        claude_pid = 10000
        table = _make_table(
            {
                my_pid: (claude_pid, "python3"),
                claude_pid: (500, "claude"),
                500: (1, "/sbin/launchd"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == claude_pid

    def test_intermediate_child_process(self) -> None:
        """MCP server under intermediate claude child (DES-011a scenario)."""
        my_pid = os.getpid()
        child_claude = 57369
        main_claude = 19147
        table = _make_table(
            {
                my_pid: (child_claude, "python3"),
                child_claude: (main_claude, "claude"),
                main_claude: (500, "claude"),
                500: (1, "/sbin/launchd"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == main_claude

    def test_single_claude_with_intermediate_zsh(self) -> None:
        """Claude spawned from a shell — only one claude in the chain."""
        my_pid = os.getpid()
        claude_pid = 57369
        shell_pid = 19147
        table = _make_table(
            {
                my_pid: (claude_pid, "python3"),
                claude_pid: (shell_pid, "claude"),
                shell_pid: (19146, "-zsh"),
                19146: (1, "/sbin/launchd"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == claude_pid

    def test_no_claude_ancestor(self) -> None:
        """No claude in the tree — falls back to os.getppid()."""
        my_pid = os.getpid()
        table = _make_table(
            {
                my_pid: (500, "python3"),
                500: (1, "zsh"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == os.getppid()

    def test_ps_failure(self) -> None:
        """ps command fails — falls back to os.getppid()."""
        with patch(
            "biff.session_key._read_process_table",
            side_effect=OSError("ps not found"),
        ):
            assert _walk_to_topmost_claude() == os.getppid()

    def test_current_process_not_in_table(self) -> None:
        """Current PID missing from table — falls back."""
        table = _make_table({999: (1, "init")})
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == os.getppid()

    def test_safety_bound_prevents_infinite_loop(self) -> None:
        """Circular parent chain doesn't hang — bounded to 10 levels."""
        my_pid = os.getpid()
        # Build a chain longer than 10 levels, all non-claude
        table: dict[int, tuple[int, str]] = {}
        pids = list(range(my_pid, my_pid + 15))
        for i, pid in enumerate(pids[:-1]):
            table[pid] = (pids[i + 1], "python3")
        table[pids[-1]] = (pids[-1], "python3")  # self-referential root
        with patch("biff.session_key._read_process_table", return_value=table):
            assert _walk_to_topmost_claude() == os.getppid()


class TestFindSessionKey:
    """Test the public API including caching."""

    def setup_method(self) -> None:
        session_key_mod._cached_key = None

    def teardown_method(self) -> None:
        session_key_mod._cached_key = None

    def test_returns_claude_ancestor(self) -> None:
        my_pid = os.getpid()
        claude_pid = 10000
        table = _make_table(
            {
                my_pid: (claude_pid, "python3"),
                claude_pid: (500, "claude"),
                500: (1, "launchd"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table):
            assert find_session_key() == claude_pid

    def test_caches_result(self) -> None:
        my_pid = os.getpid()
        table = _make_table(
            {
                my_pid: (10000, "python3"),
                10000: (500, "claude"),
                500: (1, "launchd"),
            }
        )
        with patch("biff.session_key._read_process_table", return_value=table) as mock:
            first = find_session_key()
            second = find_session_key()
        assert first == second == 10000
        mock.assert_called_once()  # ps only called once

    def test_fallback_when_no_claude(self) -> None:
        table = _make_table({os.getpid(): (500, "python3"), 500: (1, "zsh")})
        with patch("biff.session_key._read_process_table", return_value=table):
            assert find_session_key() == os.getppid()
