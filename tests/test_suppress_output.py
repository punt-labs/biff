"""Tests for plugin/hooks/suppress-output.sh — PostToolUse display hook.

Invokes the shell script via subprocess with JSON on stdin,
verifies the JSON output matches expected panel summaries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

HOOK = str(
    Path(__file__).resolve().parent.parent / "plugin" / "hooks" / "suppress-output.sh"
)


def _run_hook(tool_name: str, tool_response: str) -> dict[str, object]:
    """Run suppress-output.sh with the given tool name and response.

    Returns the parsed JSON output from the hook.
    """
    payload = json.dumps(
        {
            "tool_name": tool_name,
            "tool_response": tool_response,
        }
    )
    result = subprocess.run(  # noqa: S603
        ["bash", HOOK],  # noqa: S607
        input=payload,
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _panel_summary(output: dict[str, object]) -> str:
    """Extract the updatedMCPToolOutput from hook JSON."""
    hook_out = cast("dict[str, object]", output["hookSpecificOutput"])
    return str(hook_out["updatedMCPToolOutput"])


class TestReadMessagesCount:
    """read_messages row counting — reads the count format_read/_dual emits.

    biff-9cz: the panel count is parsed from the tool's own "N new
    message(s)" header line(s), never recounted from data rows — a row
    count also matches the (now differently indented) column-header row,
    and cannot in general distinguish a data row from a message body that
    happens to share the same indent. Fixtures below mirror the real
    format_read/format_read_dual output shape exactly.
    """

    def test_data_rows_counted(self) -> None:
        """The count line's number is read, not the row count."""
        response = (
            "\u25b6  2 new messages\n"
            "   FROM              DATE              MESSAGE\n"
            "   kai:tty01         Mon Mar 31 14:00  hello\n"
            "   eric:tty02        Mon Mar 31 14:05  world"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "2 new"

    def test_single_row(self) -> None:
        """Singular count line produces '1 new'."""
        response = (
            "\u25b6  1 new message\n"
            "   FROM              DATE              MESSAGE\n"
            "   kai:tty01         Mon Mar 31 14:00  ping"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "1 new"

    def test_no_new_messages(self) -> None:
        """'No new messages.' produces simple panel output."""
        output = _run_hook("mcp__plugin_biff_tty__read_messages", "No new messages.")
        summary = _panel_summary(output)
        assert summary == "No new messages."

    def test_could_not_check_mail_is_not_reported_as_zero_new(self) -> None:
        """biff-brn: a failure must not render like a confirmed-empty inbox.

        Before this branch, a persistent-failure result would have fallen
        into the counting branch and, matching no '^▶' lines, reported
        '0 new' — indistinguishable in the panel from a genuinely empty
        inbox. It must instead be surfaced as a failure.
        """
        response = (
            "Could not check mail — failed twice (nats: timeout). "
            "Inbox state unknown, not confirmed empty."
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "check failed"
        assert summary != "0 new"

    def test_user_without_tty(self) -> None:
        """Rows with user (no tty) are also counted correctly."""
        response = (
            "\u25b6  1 new message\n"
            "   FROM              DATE              MESSAGE\n"
            "   kai               Mon Mar 31 14:00  hello"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "1 new"

    def test_dual_section_counts_summed(self) -> None:
        """format_read_dual's per-identity counts sum to the panel total."""
        response = (
            "\u25b6  jfreeman (1 new message)\n"
            "   FROM   DATE              MESSAGE\n"
            "   kai    Mon Mar 31 14:00  hey\n"
            "\n"
            "\u25b6  claude (2 new messages)\n"
            "   FROM   DATE              MESSAGE\n"
            "   rmh    Mon Mar 31 14:00  done\n"
            "   alpha  Mon Mar 31 14:01  ping"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "3 new"


class TestWhoCount:
    """who row counting excludes the dead-session footnote (DES-057)."""

    def test_dead_footnote_not_counted_as_online(self) -> None:
        """The footnote shares ROW_PREFIX with live rows but is not a session."""
        response = (
            "▶  NAME  IDLE\n"
            "   kai:tty01  0:03\n"
            "   1 session stopped responding (last seen 3 hours)"
        )
        output = _run_hook("mcp__plugin_biff_tty__who", response)
        summary = _panel_summary(output)
        assert summary == "1 online"

    def test_all_dead_reports_zero_online(self) -> None:
        """When only orphans remain, the online count must not inflate to 1+."""
        response = "No sessions.\n   2 sessions stopped responding (last seen 3 hours)"
        output = _run_hook("mcp__plugin_biff_tty__who", response)
        summary = _panel_summary(output)
        assert summary == "0 online"

    def test_wrapped_footnote_continuation_not_counted(self) -> None:
        """A footnote long enough to wrap must not inflate the count either.

        Continuation lines share ROW_PREFIX but never contain "stopped
        responding" themselves -- a line-by-line filter on that phrase alone
        would miss them (Cursor Bugbot, Medium).
        """
        response = (
            "▶  NAME  IDLE\n"
            "   kai:tty01  0:03\n"
            "   3 sessions stopped responding (last seen 3 hours, 1 day 2\n"
            "   hours, 5 days)"
        )
        output = _run_hook("mcp__plugin_biff_tty__who", response)
        summary = _panel_summary(output)
        assert summary == "1 online"
