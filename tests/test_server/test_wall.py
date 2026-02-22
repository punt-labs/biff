"""Unit tests for /wall — model, duration parsing, and formatting."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from biff.models import WallPost
from biff.server.tools.wall import _parse_duration, format_remaining


class TestWallPostModel:
    def test_create(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="release freeze",
            from_user="kai",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        assert post.text == "release freeze"
        assert post.from_user == "kai"
        assert not post.is_expired

    def test_expired(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="old wall",
            from_user="kai",
            posted_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        assert post.is_expired

    def test_frozen(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="test",
            from_user="kai",
            expires_at=now + timedelta(hours=1),
        )
        with pytest.raises(ValidationError):
            post.text = "changed"  # pyright: ignore[reportAttributeAccessIssue]

    def test_rejects_empty_text(self) -> None:
        with pytest.raises(ValidationError):
            WallPost(
                text="",
                from_user="kai",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

    def test_rejects_long_text(self) -> None:
        with pytest.raises(ValidationError):
            WallPost(
                text="x" * 201,
                from_user="kai",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

    def test_json_roundtrip(self) -> None:
        now = datetime.now(UTC)
        post = WallPost(
            text="deploy freeze",
            from_user="eric",
            posted_at=now,
            expires_at=now + timedelta(hours=2),
        )
        parsed = WallPost.model_validate_json(post.model_dump_json())
        assert parsed == post


class TestParseDuration:
    def test_empty_returns_default(self) -> None:
        assert _parse_duration("") == timedelta(hours=1)

    def test_minutes(self) -> None:
        assert _parse_duration("30m") == timedelta(minutes=30)

    def test_hours(self) -> None:
        assert _parse_duration("2h") == timedelta(hours=2)

    def test_days(self) -> None:
        assert _parse_duration("3d") == timedelta(days=3)

    def test_case_insensitive(self) -> None:
        assert _parse_duration("2H") == timedelta(hours=2)

    def test_strips_whitespace(self) -> None:
        assert _parse_duration("  30m  ") == timedelta(minutes=30)

    def test_rejects_invalid_unit(self) -> None:
        with pytest.raises(ValueError, match="Unrecognized"):
            _parse_duration("10x")

    def test_rejects_non_numeric(self) -> None:
        with pytest.raises(ValueError, match="Unrecognized"):
            _parse_duration("abch")

    def test_rejects_zero(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _parse_duration("0h")

    def test_rejects_negative(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _parse_duration("-1h")

    def test_rejects_exceeds_max(self) -> None:
        with pytest.raises(ValueError, match="maximum"):
            _parse_duration("4d")


class TestFormatRemaining:
    def test_hours_and_minutes(self) -> None:
        # Add buffer to avoid sub-second drift between now() calls
        expires = datetime.now(UTC) + timedelta(hours=1, minutes=30, seconds=30)
        result = format_remaining(expires)
        assert "1h" in result
        assert "30m" in result

    def test_minutes_only(self) -> None:
        expires = datetime.now(UTC) + timedelta(minutes=45, seconds=30)
        result = format_remaining(expires)
        assert "45m" in result
        assert "h" not in result

    def test_expired(self) -> None:
        expires = datetime.now(UTC) - timedelta(minutes=5)
        assert format_remaining(expires) == "expired"

    def test_less_than_one_minute(self) -> None:
        expires = datetime.now(UTC) + timedelta(seconds=30)
        assert format_remaining(expires) == "<1m"
