"""Tests for the shared formatting module (biff.formatting).

Verifies domain-level format functions produce correct output.
These functions are shared by both MCP tools and CLI commands.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from biff.formatting import (
    format_finger,
    format_finger_multi,
    format_last,
    format_read,
    format_wall,
    format_who,
    pair_events,
    parse_duration,
    sanitize_wall_message,
    terminal_safe,
)
from biff.models import Message, SessionEvent, UserSession, WallPost


class TestFormatWho:
    def test_single_session(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            plan="working on biff",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "kai" in result
        assert "tty1" in result
        assert "@kai" not in result  # no @ prefix

    def test_empty_sessions(self):
        result = format_who([])
        # Header-only table — no data rows, but no crash
        assert "NAME" in result
        assert "@" not in result

    def test_plan_flag_column(self):
        with_plan = UserSession(
            user="kai", tty="abcd1234", plan="coding", last_active=datetime.now(UTC)
        )
        without_plan = UserSession(
            user="eric", tty="efgh5678", last_active=datetime.now(UTC)
        )
        result = format_who([with_plan, without_plan])
        assert "P" in result.splitlines()[0]  # header
        lines = result.splitlines()
        # kai has a plan → "+"
        kai_line = next(line for line in lines if "kai" in line and "eric" not in line)
        eric_line = next(line for line in lines if "eric" in line)
        # P column is after S column; both have "+" for S (biff_enabled default)
        # so kai's line has two consecutive "+" (S and P), eric has "+" then "-"
        assert "+  +" in kai_line  # S=+, P=+
        assert "+  -" in eric_line  # S=+, P=-


class TestFormatWhoKindTags:
    def test_agent_shows_tag(self):
        session = UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty1",
            kind="agent",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" in result

    def test_human_no_tag(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="human",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" not in result

    def test_empty_kind_no_tag(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "[A]" not in result


class TestFormatFinger:
    def test_single_session(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="main",
            plan="debugging relay",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: kai" in result
        assert "main" in result
        assert "debugging relay" in result

    def test_multi_session(self):
        s1 = UserSession(
            user="kai",
            tty="aaaa1111",
            tty_name="tty1",
            last_active=datetime.now(UTC),
        )
        s2 = UserSession(
            user="kai",
            tty="bbbb2222",
            tty_name="tty2",
            last_active=datetime.now(UTC) - timedelta(minutes=5),
        )
        result = format_finger_multi([s1, s2])
        assert "Login: kai" in result
        assert "tty1" in result
        assert "tty2" in result

    def test_finger_shows_kind(self):
        session = UserSession(
            user="claude",
            tty="abcd1234",
            tty_name="tty1",
            kind="agent",
            display_name="Claude Agento",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: claude [agent]" in result

    def test_finger_no_kind_when_empty(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            tty_name="tty1",
            kind="",
            last_active=datetime.now(UTC),
        )
        result = format_finger(session)
        assert "Login: kai" in result
        assert "[" not in result.split("\n")[0]


class TestParseDuration:
    def test_default_empty(self):
        assert parse_duration("") == timedelta(hours=1)

    def test_minutes(self):
        assert parse_duration("30m") == timedelta(minutes=30)

    def test_hours(self):
        assert parse_duration("2h") == timedelta(hours=2)

    def test_days(self):
        assert parse_duration("3d") == timedelta(days=3)


class TestSanitizeWallMessage:
    def test_strips_control_chars(self):
        result = sanitize_wall_message("hello\x00world")
        assert result == "helloworld"

    def test_collapses_whitespace(self):
        result = sanitize_wall_message("hello   world  foo")
        assert result == "hello world foo"


class TestFormatWall:
    def test_basic_wall(self):
        now = datetime.now(UTC)
        wall = WallPost(
            text="standup in 5",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        result = format_wall(wall)
        assert "WALL" in result
        assert "kai" in result
        assert "@kai" not in result
        assert "standup in 5" in result


class TestPairEvents:
    def test_pairs_login_logout(self):
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            timestamp=now - timedelta(hours=1),
        )
        logout = SessionEvent(
            session_key="kai:tty1",
            event="logout",
            user="kai",
            timestamp=now,
        )
        pairs = pair_events([login, logout])
        assert len(pairs) == 1
        assert pairs[0][0] == login
        assert pairs[0][1] == logout

    def test_unpaired_login(self):
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            timestamp=now,
        )
        pairs = pair_events([login])
        assert len(pairs) == 1
        assert pairs[0][1] is None


class TestFormatRead:
    def test_basic_messages(self):
        m = Message(from_user="kai", to_user="eric", body="hey there")
        result = format_read([m])
        assert "kai" in result
        assert "@kai" not in result
        assert "hey there" in result


class TestTerminalSafe:
    """`terminal_safe` strips control/escape chars from remote text (biff-lbj)."""

    def test_strips_esc_and_bel(self) -> None:
        assert terminal_safe("a\x1b[2Jb\x07c") == "a[2Jbc"

    def test_strips_newline_and_cr(self) -> None:
        # A single-line render must not be splittable by embedded newlines.
        assert terminal_safe("line1\nline2\rline3") == "line1line2line3"

    def test_preserves_printable_unicode(self) -> None:
        assert terminal_safe("kai:tty2 ▶ 🚀 café") == "kai:tty2 ▶ 🚀 café"

    def test_empty_string(self) -> None:
        assert terminal_safe("") == ""


class TestRenderSanitization:
    """Remote terminal escapes are neutralized at every render site (biff-lbj).

    Each render path is fed a relay-sourced field carrying a screen-clear
    (`\\x1b[2J`) and assert the raw escape never reaches the output.
    """

    def test_read_body_and_sender(self) -> None:
        m = Message(from_user="e\x1b[2Kvil", to_user="kai", body="hi\x1b[2Jthere")
        out = format_read([m])
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "hi[2Jthere" in out

    def test_wall_text_and_sender(self) -> None:
        wall = WallPost(
            from_user="ev\x1b[2Kil",
            from_tty="tty\x1b[2K9",
            text="dep\x1b[2Jloy freeze",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        out = format_wall(wall)
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "dep[2Jloy freeze" in out

    def test_who_name_and_host(self) -> None:
        s = UserSession(user="ka\x1b[2Ji", tty="abc12345", hostname="ho\x1b[2Kst")
        out = format_who([s])
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out

    def test_finger_plan_host_pwd(self) -> None:
        s = UserSession(
            user="kai",
            tty="abc12345",
            plan="pl\x1b[2Jan",
            hostname="h\x1b[2Kost",
            pwd="/co\x1b[2Kde",
        )
        out = format_finger(s)
        assert "\x1b[2J" not in out
        assert "\x1b[2K" not in out
        assert "pl[2Jan" in out

    def test_last_user_and_host(self) -> None:
        login = SessionEvent(
            session_key="evil:tty9",
            event="login",
            user="ev\x1b[2Kil",
            tty="tty9",
            hostname="h\x1b[2Kost",
        )
        out = format_last([(login, None)], {"evil:tty9"})
        assert "\x1b[2K" not in out
        assert re.search(r"ev\[2Kil", out) is not None
