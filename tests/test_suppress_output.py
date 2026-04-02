"""Tests for hooks/suppress-output.sh — PostToolUse display hook.

Invokes the shell script via subprocess with JSON on stdin,
verifies the JSON output matches expected panel summaries.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import cast

HOOK = str(Path(__file__).resolve().parent.parent / "hooks" / "suppress-output.sh")


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
    """read_messages row counting — the @ prefix bug and fix."""

    def test_at_prefix_rows_counted(self) -> None:
        """Rows starting with @user:tty are counted correctly."""
        # Simulate format_read() output: header + 2 data rows with @ prefix
        response = (
            "\u25b6  FROM              DATE              BODY\n"
            "   @kai:tty01        Mon Mar 31 14:00  hello\n"
            "   @eric:tty02       Mon Mar 31 14:05  world"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "2 new"

    def test_single_at_row(self) -> None:
        """Single message row produces '1 new'."""
        response = (
            "\u25b6  FROM              DATE              BODY\n"
            "   @kai:tty01        Mon Mar 31 14:00  ping"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "1 new"

    def test_no_new_messages(self) -> None:
        """'No new messages.' produces simple panel output."""
        output = _run_hook("mcp__plugin_biff_tty__read_messages", "No new messages.")
        summary = _panel_summary(output)
        assert summary == "No new messages."

    def test_at_without_tty(self) -> None:
        """Rows with @user (no tty) are also counted."""
        response = (
            "\u25b6  FROM              DATE              BODY\n"
            "   @kai              Mon Mar 31 14:00  hello"
        )
        output = _run_hook("mcp__plugin_biff_tty__read_messages", response)
        summary = _panel_summary(output)
        assert summary == "1 new"
