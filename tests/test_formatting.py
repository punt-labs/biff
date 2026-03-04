"""Tests for the shared formatting module (biff.formatting).

Verifies domain-level format functions produce correct output.
These functions are shared by both MCP tools and CLI commands.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.formatting import (
    format_finger,
    format_finger_multi,
    format_read,
    format_wall,
    format_who,
    pair_events,
    parse_duration,
    sanitize_wall_message,
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
        assert "@kai" in result
        assert "tty1" in result
        assert "working on biff" in result

    def test_empty_sessions(self):
        result = format_who([])
        # Header-only table — no data rows, but no crash
        assert "NAME" in result
        assert "@" not in result

    def test_no_plan(self):
        session = UserSession(
            user="kai",
            tty="abcd1234",
            last_active=datetime.now(UTC),
        )
        result = format_who([session])
        assert "(no plan)" in result


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
        assert "@kai" in result
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
        assert "hey there" in result
