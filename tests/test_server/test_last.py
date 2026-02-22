"""Unit tests for the /last tool — formatting and model logic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from biff.models import SessionEvent
from biff.server.tools.last import (
    _format_duration,
    _format_table,
    _format_timestamp,
    _pair_events,
)


class TestSessionEventModel:
    def test_login_event(self) -> None:
        ts = datetime.now(UTC)
        event = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            tty_name="tty1",
            hostname="dev-box",
            pwd="/home/kai",
            timestamp=ts,
            plan="coding",
        )
        assert event.event == "login"
        assert event.user == "kai"
        assert event.session_key == "kai:tty1"

    def test_logout_event(self) -> None:
        event = SessionEvent(
            session_key="kai:tty1",
            event="logout",
            user="kai",
            tty="tty1",
            timestamp=datetime.now(UTC),
        )
        assert event.event == "logout"

    def test_rejects_invalid_event_type(self) -> None:
        with pytest.raises(ValidationError):
            SessionEvent(
                session_key="kai:tty1",
                event="invalid",
                user="kai",
                tty="tty1",
                timestamp=datetime.now(UTC),
            )

    def test_frozen(self) -> None:
        event = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=datetime.now(UTC),
        )
        with pytest.raises(ValidationError):
            event.user = "eric"  # pyright: ignore[reportAttributeAccessIssue]

    def test_json_roundtrip(self) -> None:
        event = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            tty_name="tty1",
            hostname="dev-box",
            timestamp=datetime.now(UTC),
        )
        json_str = event.model_dump_json()
        parsed = SessionEvent.model_validate_json(json_str)
        assert parsed == event


class TestFormatDuration:
    def test_zero_duration(self) -> None:
        ts = datetime.now(UTC)
        assert _format_duration(ts, ts) == "0:00"

    def test_minutes_only(self) -> None:
        ts = datetime.now(UTC)
        assert _format_duration(ts, ts + timedelta(minutes=45)) == "0:45"

    def test_hours_and_minutes(self) -> None:
        ts = datetime.now(UTC)
        assert _format_duration(ts, ts + timedelta(hours=3, minutes=22)) == "3:22"

    def test_large_duration(self) -> None:
        ts = datetime.now(UTC)
        assert _format_duration(ts, ts + timedelta(hours=10, minutes=5)) == "10:05"


class TestFormatTimestamp:
    def test_format(self) -> None:
        ts = datetime(2026, 2, 22, 14, 1, tzinfo=UTC)
        result = _format_timestamp(ts)
        assert "Feb" in result
        assert "22" in result
        assert "14:01" in result


class TestPairEvents:
    def test_login_with_matching_logout(self) -> None:
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now - timedelta(hours=3),
        )
        logout = SessionEvent(
            session_key="kai:tty1",
            event="logout",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        pairs = _pair_events([logout, login], set())
        assert len(pairs) == 1
        assert pairs[0][0].event == "login"
        assert pairs[0][1] is not None
        assert pairs[0][1].event == "logout"

    def test_still_logged_in(self) -> None:
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        pairs = _pair_events([login], {"kai:tty1"})
        assert len(pairs) == 1
        assert pairs[0][1] is None  # No logout — still logged in

    def test_multiple_sessions(self) -> None:
        now = datetime.now(UTC)
        events = [
            SessionEvent(
                session_key="kai:tty1",
                event="logout",
                user="kai",
                tty="tty1",
                timestamp=now,
            ),
            SessionEvent(
                session_key="kai:tty1",
                event="login",
                user="kai",
                tty="tty1",
                timestamp=now - timedelta(hours=3),
            ),
            SessionEvent(
                session_key="eric:tty2",
                event="login",
                user="eric",
                tty="tty2",
                timestamp=now - timedelta(hours=1),
            ),
        ]
        pairs = _pair_events(events, {"eric:tty2"})
        assert len(pairs) == 2


class TestFormatTable:
    def test_empty_returns_message(self) -> None:
        assert _format_table([], set()) == "No session history."

    def test_single_completed_session(self) -> None:
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            tty_name="tty1",
            hostname="dev-box",
            timestamp=now - timedelta(hours=3, minutes=22),
        )
        logout = SessionEvent(
            session_key="kai:tty1",
            event="logout",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        result = _format_table([(login, logout)], set())
        assert "@kai" in result
        assert "tty1" in result
        assert "dev-box" in result
        assert "3:22" in result
        assert "\u25b6" in result  # Header marker

    def test_still_logged_in_session(self) -> None:
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        result = _format_table([(login, None)], {"kai:tty1"})
        assert "still logged in" in result
        assert "-" in result  # Duration is "-"

    def test_gone_session(self) -> None:
        """Session with login but no logout and not active shows 'gone'."""
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        result = _format_table([(login, None)], set())
        assert "gone" in result

    def test_header_columns(self) -> None:
        now = datetime.now(UTC)
        login = SessionEvent(
            session_key="kai:tty1",
            event="login",
            user="kai",
            tty="tty1",
            timestamp=now,
        )
        result = _format_table([(login, None)], set())
        assert "NAME" in result
        assert "TTY" in result
        assert "HOST" in result
        assert "LOGIN" in result
        assert "LOGOUT" in result
        assert "DURATION" in result
