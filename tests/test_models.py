"""Tests for biff data models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from biff.models import BiffConfig, Message, UnreadSummary, UserSession


class TestMessage:
    def test_create_with_defaults(self) -> None:
        msg = Message(from_user="kai", to_user="eric", body="auth ready")
        assert msg.from_user == "kai"
        assert msg.to_user == "eric"
        assert msg.body == "auth ready"
        assert msg.read is False
        assert isinstance(msg.id, uuid.UUID)
        assert msg.timestamp.tzinfo is not None

    def test_create_with_explicit_fields(self) -> None:
        ts = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        msg_id = uuid.uuid4()
        msg = Message(
            id=msg_id,
            from_user="kai",
            to_user="eric",
            body="hello",
            timestamp=ts,
            read=True,
        )
        assert msg.id == msg_id
        assert msg.timestamp == ts
        assert msg.read is True

    def test_frozen(self) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        with pytest.raises(ValidationError):
            msg.body = "changed"

    def test_unique_ids(self) -> None:
        a = Message(from_user="kai", to_user="eric", body="one")
        b = Message(from_user="kai", to_user="eric", body="two")
        assert a.id != b.id

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError, match="body"):
            Message(from_user="kai", to_user="eric", body="")

    def test_whitespace_body_rejected(self) -> None:
        with pytest.raises(ValidationError, match="body"):
            Message(from_user="kai", to_user="eric", body="   ")

    def test_empty_from_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="from_user"):
            Message(from_user="", to_user="eric", body="hello")

    def test_whitespace_from_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="from_user"):
            Message(from_user="  ", to_user="eric", body="hello")

    def test_empty_to_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="to_user"):
            Message(from_user="kai", to_user="", body="hello")

    def test_whitespace_to_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="to_user"):
            Message(from_user="kai", to_user="  ", body="hello")

    def test_strings_are_stripped(self) -> None:
        msg = Message(from_user="  kai  ", to_user="  eric  ", body="  hi  ")
        assert msg.from_user == "kai"
        assert msg.to_user == "eric"
        assert msg.body == "hi"

    def test_json_round_trip(self) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        json_str = msg.model_dump_json()
        restored = Message.model_validate_json(json_str)
        assert restored == msg

    def test_timestamp_is_utc(self) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        assert msg.timestamp.tzinfo == UTC

    def test_non_utc_timestamp_normalized(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        ts = datetime(2026, 2, 13, 12, 0, tzinfo=eastern)
        msg = Message(from_user="kai", to_user="eric", body="hi", timestamp=ts)
        assert msg.timestamp.tzinfo is UTC or msg.timestamp.utcoffset() == timedelta(0)
        assert msg.timestamp == ts.astimezone(UTC)

    def test_naive_timestamp_rejected(self) -> None:
        naive = datetime(2026, 2, 13, 12, 0)
        with pytest.raises(ValidationError, match="timezone"):
            Message(from_user="kai", to_user="eric", body="hi", timestamp=naive)


class TestUserSession:
    def test_create_with_defaults(self) -> None:
        session = UserSession(user="kai")
        assert session.user == "kai"
        assert session.plan == ""
        assert session.biff_enabled is True
        assert session.last_active.tzinfo is not None

    def test_create_with_plan(self) -> None:
        session = UserSession(user="kai", plan="refactoring auth")
        assert session.plan == "refactoring auth"

    def test_biff_disabled(self) -> None:
        session = UserSession(user="kai", biff_enabled=False)
        assert session.biff_enabled is False

    def test_frozen(self) -> None:
        session = UserSession(user="kai")
        with pytest.raises(ValidationError):
            session.plan = "changed"

    def test_empty_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            UserSession(user="")

    def test_whitespace_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            UserSession(user="   ")

    def test_json_round_trip(self) -> None:
        session = UserSession(user="kai", plan="working on tests")
        json_str = session.model_dump_json()
        restored = UserSession.model_validate_json(json_str)
        assert restored == session

    def test_last_active_is_utc(self) -> None:
        session = UserSession(user="kai")
        assert session.last_active.tzinfo == UTC

    def test_non_utc_last_active_normalized(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        ts = datetime(2026, 2, 13, 12, 0, tzinfo=eastern)
        session = UserSession(user="kai", last_active=ts)
        assert session.last_active == ts.astimezone(UTC)

    def test_naive_last_active_rejected(self) -> None:
        naive = datetime(2026, 2, 13, 12, 0)
        with pytest.raises(ValidationError, match="timezone"):
            UserSession(user="kai", last_active=naive)


class TestBiffConfig:
    def test_create_minimal(self) -> None:
        config = BiffConfig(user="kai")
        assert config.user == "kai"
        assert config.relay_url is None
        assert config.team == ()

    def test_create_full(self) -> None:
        config = BiffConfig(
            user="kai",
            relay_url="ws://localhost:8420",
            team=("kai", "eric", "jess"),
        )
        assert config.relay_url == "ws://localhost:8420"
        assert config.team == ("kai", "eric", "jess")

    def test_team_is_tuple(self) -> None:
        config = BiffConfig(user="kai", team=["eric", "jess"])  # type: ignore[arg-type]
        assert isinstance(config.team, tuple)

    def test_frozen(self) -> None:
        config = BiffConfig(user="kai")
        with pytest.raises(ValidationError):
            config.user = "changed"

    def test_empty_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            BiffConfig(user="")

    def test_whitespace_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            BiffConfig(user="   ")

    def test_json_round_trip(self) -> None:
        config = BiffConfig(
            user="kai",
            relay_url="ws://localhost:8420",
            team=("eric", "jess"),
        )
        json_str = config.model_dump_json()
        restored = BiffConfig.model_validate_json(json_str)
        assert restored == config


class TestUnreadSummary:
    def test_empty(self) -> None:
        summary = UnreadSummary()
        assert summary.count == 0
        assert summary.preview == ""

    def test_with_messages(self) -> None:
        summary = UnreadSummary(count=2, preview="@kai about auth, @eric about lunch")
        assert summary.count == 2
        assert "@kai" in summary.preview

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValidationError, match="count"):
            UnreadSummary(count=-1)

    def test_frozen(self) -> None:
        summary = UnreadSummary(count=1, preview="@kai about auth")
        with pytest.raises(ValidationError):
            summary.count = 0
