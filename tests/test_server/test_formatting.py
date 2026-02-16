"""Tests for shared formatting helpers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from biff.server.tools._formatting import format_idle


class TestFormatIdle:
    def test_zero_minutes(self) -> None:
        now = datetime.now(UTC)
        assert format_idle(now) == "0m"

    def test_minutes(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=45)
        assert format_idle(dt) == "45m"

    def test_hours(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=3, minutes=15)
        assert format_idle(dt) == "3h"

    def test_days(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=2, hours=5)
        assert format_idle(dt) == "2d"

    def test_boundary_59_minutes(self) -> None:
        dt = datetime.now(UTC) - timedelta(minutes=59)
        assert format_idle(dt) == "59m"

    def test_boundary_1_hour(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=1)
        assert format_idle(dt) == "1h"

    def test_boundary_23_hours(self) -> None:
        dt = datetime.now(UTC) - timedelta(hours=23, minutes=59)
        assert format_idle(dt) == "23h"

    def test_boundary_1_day(self) -> None:
        dt = datetime.now(UTC) - timedelta(days=1)
        assert format_idle(dt) == "1d"

    def test_future_timestamp_returns_zero(self) -> None:
        dt = datetime.now(UTC) + timedelta(hours=1)
        assert format_idle(dt) == "0m"
