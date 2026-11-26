"""Tests for plugin/hooks/suppress-output.sh — PostToolUse display hook.

Invokes the shell script via subprocess with JSON on stdin,
verifies the JSON output matches expected panel summaries.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import biff.commands.talk as talk_commands
from biff.cli_context import CliContext
from biff.commands._messaging import Delivery
from biff.formatting import (
    format_talk_echo,
    format_wall_confirmation,
    format_who,
)
from biff.models import BiffConfig, UserSession, WallPost
from biff.relay import LocalRelay
from biff.server.tools._write_target import WriteRouter

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
    # A throwaway HOME: the operator's ~/.punt-hooks-kill must not change results.
    with tempfile.TemporaryDirectory() as home:
        result = subprocess.run(  # noqa: S603
            ["bash", HOOK],  # noqa: S607
            input=payload,
            capture_output=True,
            text=True,
            timeout=5,
            env={**os.environ, "HOME": home},
        )
    assert result.returncode == 0, f"Hook failed: {result.stderr}"
    return json.loads(result.stdout)  # type: ignore[no-any-return]


def _panel_summary(output: dict[str, object]) -> str:
    """Extract the updatedMCPToolOutput from hook JSON."""
    hook_out = cast("dict[str, object]", output["hookSpecificOutput"])
    return str(hook_out["updatedMCPToolOutput"])


class TestReadMessagesCount:
    """read_messages row counting — reads the count format_read emits.

    The panel count is parsed from the tool's own "N new
    message(s)" header line(s), never recounted from data rows — a row
    count also matches the (now differently indented) column-header row,
    and cannot in general distinguish a data row from a message body that
    happens to share the same indent. Fixtures below mirror the real
    format_read output shape exactly.
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
        """Surface an inbox failure rather than reporting zero new messages."""
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


class TestWhoCount:
    """The who panel line counts the rows of the table /who prints."""

    def test_each_live_row_counts_once(self) -> None:
        now = datetime.now(UTC)
        table = format_who(
            [
                UserSession(user="kai", tty="aa", tty_name="tty1", last_active=now),
                UserSession(user="eric", tty="bb", tty_name="tty2", last_active=now),
            ]
        )
        output = _run_hook("mcp__plugin_biff_tty__who", table)
        assert _panel_summary(output) == "2 online"

    def test_an_older_servers_wrapped_footnote_is_not_counted(self) -> None:
        """A plugin-only update can pair this hook with a server that still
        appends the dead-session footnote; its wrapped lines share the row
        prefix and must not count as online."""
        response = (
            "▶  NAME  IDLE\n"
            "   kai:tty01  0:03\n"
            "   3 sessions stopped responding (last seen 3 hours, 1 day 2\n"
            "   hours, 5 days)"
        )
        output = _run_hook("mcp__plugin_biff_tty__who", response)
        assert _panel_summary(output) == "1 online"

    def test_no_sessions_is_passed_through(self) -> None:
        output = _run_hook("mcp__plugin_biff_tty__who", "No sessions.")
        assert _panel_summary(output) == "No sessions."


class TestWriteForwardNotice:
    """A forwarded write keeps its notice; a plain send collapses to "sent"."""

    def test_plain_send_collapses_to_sent(self) -> None:
        assert _panel_summary(_run_hook("write", "Message sent to eric.")) == "sent"

    def test_forward_notice_is_preserved(self) -> None:
        target = WriteRouter(local_repo="", visible_repos=set()).resolve(
            [UserSession(user="eric", tty="aa", tty_name="tty1")],
            "eric",
            "tty9",
        )
        notice = target.report(Delivery(text="Message sent to eric.", ok=True, parts=1))
        assert _panel_summary(_run_hook("write", notice)) == notice


def _context(output: dict[str, object]) -> str:
    """Extract the additionalContext (what the model reads) from hook JSON."""
    hook_out = cast("dict[str, object]", output["hookSpecificOutput"])
    return str(hook_out.get("additionalContext", ""))


async def _real_invite_confirmation(tmp_path: Path) -> str:
    """Run the real talk invite command and return its confirmation text."""
    relay = LocalRelay(tmp_path)
    await relay.update_session(
        UserSession(user="eric", tty="erichex1", tty_name="tty6", repo="test")
    )
    kai = CliContext(
        relay=relay,
        config=BiffConfig(user="kai", repo_name="test"),
        session_key="kai:kaihex01",
        user="kai",
        tty="kaihex01",
        tty_name="tty1",
    )
    result = await talk_commands.invite(
        kai,
        user="eric",
        relay_key="eric:erichex1",
        display="eric:tty6",
        resolve_tty="tty6",
        message="",
    )
    assert not result.error
    return result.text


def _first_line_cut(text: str) -> str:
    """The panel line the hook keeps: the first line, at most 60 characters."""
    return text.splitlines()[0][:60]


class TestConfirmationsReachTheModel:
    """talk, plan and wall keep a short panel line; the model gets every word."""

    async def test_a_long_talk_confirmation_reaches_the_model_whole(
        self, tmp_path: Path
    ) -> None:
        text = await _real_invite_confirmation(tmp_path)
        assert len(text) > 60  # the cut this fix is about
        output = _run_hook("talk", text)
        assert _panel_summary(output) == _first_line_cut(text)
        assert _context(output) == text

    def test_a_two_line_plan_echo_keeps_the_plan_text(self) -> None:
        text = format_talk_echo("Plan:", "rewrite the relay reconnect loop")
        assert "\n" in text  # the echo puts the plan on its own line
        output = _run_hook("plan", text)
        assert _panel_summary(output) == _first_line_cut(text)
        assert _context(output) == text
        assert "rewrite the relay reconnect loop" in _context(output)

    def test_a_wall_confirmation_keeps_its_body(self) -> None:
        post = WallPost(
            text="deploy freeze until the release is tagged and verified",
            from_user="kai",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )
        text = format_wall_confirmation(post)
        assert "\n" in text  # the body sits below the header
        output = _run_hook("wall", text)
        assert _panel_summary(output) == _first_line_cut(text)
        assert _context(output) == text
