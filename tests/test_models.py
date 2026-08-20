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

    def test_plan_source_defaults_to_manual(self) -> None:
        session = UserSession(user="kai")
        assert session.plan_source == "manual"

    def test_plan_source_manual(self) -> None:
        session = UserSession(user="kai", plan="work", plan_source="manual")
        assert session.plan_source == "manual"

    def test_plan_source_auto(self) -> None:
        session = UserSession(user="kai", plan="→ main", plan_source="auto")
        assert session.plan_source == "auto"

    def test_plan_source_invalid_rejected(self) -> None:
        with pytest.raises(ValidationError, match="plan_source"):
            UserSession(user="kai", plan_source="unknown")  # type: ignore[arg-type]

    def test_plan_source_round_trip(self) -> None:
        session = UserSession(user="kai", plan="work", plan_source="auto")
        json_str = session.model_dump_json()
        restored = UserSession.model_validate_json(json_str)
        assert restored.plan_source == "auto"

    def test_plan_source_missing_in_json_defaults_manual(self) -> None:
        """Old sessions without plan_source deserialize with 'manual' default."""
        raw = '{"user": "kai", "plan": "working"}'
        session = UserSession.model_validate_json(raw)
        assert session.plan_source == "manual"

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

    def test_last_tool_at_defaults_near_construction_time(self) -> None:
        """A fresh session (never invoked a tool) idles from its own start."""
        before = datetime.now(UTC)
        session = UserSession(user="kai")
        after = datetime.now(UTC)
        assert before <= session.last_tool_at <= after

    def test_last_tool_at_is_utc(self) -> None:
        session = UserSession(user="kai")
        assert session.last_tool_at.tzinfo == UTC

    def test_non_utc_last_tool_at_normalized(self) -> None:
        eastern = timezone(timedelta(hours=-5))
        ts = datetime(2026, 2, 13, 12, 0, tzinfo=eastern)
        session = UserSession(user="kai", last_tool_at=ts)
        assert session.last_tool_at == ts.astimezone(UTC)

    def test_naive_last_tool_at_rejected(self) -> None:
        naive = datetime(2026, 2, 13, 12, 0)
        with pytest.raises(ValidationError, match="timezone"):
            UserSession(user="kai", last_tool_at=naive)

    def test_last_tool_at_independent_of_last_active(self) -> None:
        """The two timestamps are distinct fields, not aliases of one value."""
        tool_at = datetime(2026, 2, 13, 10, 0, tzinfo=UTC)
        active_at = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        session = UserSession(user="kai", last_tool_at=tool_at, last_active=active_at)
        assert session.last_tool_at == tool_at
        assert session.last_active == active_at
        assert session.last_tool_at != session.last_active

    def test_last_tool_at_round_trip(self) -> None:
        tool_at = datetime(2026, 2, 13, 10, 0, tzinfo=UTC)
        session = UserSession(user="kai", last_tool_at=tool_at)
        restored = UserSession.model_validate_json(session.model_dump_json())
        assert restored.last_tool_at == tool_at

    def test_last_tool_at_missing_from_wire_falls_back_to_last_active(self) -> None:
        """A record written before this field existed has no last_tool_at key.

        The fallback must read the record's own last_active, not the field
        default (now()) — the latter would make a long-dead session read as
        freshly active, reproducing the exact bug this field fixes.
        """
        old = datetime(2026, 2, 13, 8, 0, tzinfo=UTC)
        raw = f'{{"user": "kai", "last_active": "{old.isoformat()}"}}'
        session = UserSession.model_validate_json(raw)
        assert session.last_tool_at == old
        assert session.last_tool_at == session.last_active

    def test_last_tool_at_present_on_wire_is_not_overwritten(self) -> None:
        """A record that already carries both fields keeps them distinct."""
        tool_at = datetime(2026, 2, 13, 9, 0, tzinfo=UTC)
        active_at = datetime(2026, 2, 13, 12, 0, tzinfo=UTC)
        raw = (
            '{"user": "kai", '
            f'"last_active": "{active_at.isoformat()}", '
            f'"last_tool_at": "{tool_at.isoformat()}"}}'
        )
        session = UserSession.model_validate_json(raw)
        assert session.last_tool_at == tool_at
        assert session.last_active == active_at


class TestBiffConfig:
    _REPO = "_test-models"

    def test_create_minimal(self) -> None:
        config = BiffConfig(user="kai", repo_name=self._REPO)
        assert config.user == "kai"
        assert config.relay_url is None
        assert config.team == ()

    def test_create_full(self) -> None:
        config = BiffConfig(
            user="kai",
            repo_name=self._REPO,
            relay_url="ws://localhost:8420",
            team=("kai", "eric", "jess"),
        )
        assert config.relay_url == "ws://localhost:8420"
        assert config.team == ("kai", "eric", "jess")

    def test_team_is_tuple(self) -> None:
        config = BiffConfig(user="kai", repo_name=self._REPO, team=["eric", "jess"])  # type: ignore[arg-type]
        assert isinstance(config.team, tuple)

    def test_frozen(self) -> None:
        config = BiffConfig(user="kai", repo_name=self._REPO)
        with pytest.raises(ValidationError):
            config.user = "changed"

    def test_empty_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            BiffConfig(user="", repo_name=self._REPO)

    def test_whitespace_user_rejected(self) -> None:
        with pytest.raises(ValidationError, match="user"):
            BiffConfig(user="   ", repo_name=self._REPO)

    def test_empty_repo_name_rejected(self) -> None:
        with pytest.raises(ValidationError, match="repo_name"):
            BiffConfig(user="kai", repo_name="")

    def test_json_round_trip(self) -> None:
        config = BiffConfig(
            user="kai",
            repo_name=self._REPO,
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

    def test_with_count(self) -> None:
        summary = UnreadSummary(count=2)
        assert summary.count == 2

    def test_negative_count_rejected(self) -> None:
        with pytest.raises(ValidationError, match="count"):
            UnreadSummary(count=-1)

    def test_frozen(self) -> None:
        summary = UnreadSummary(count=1)
        with pytest.raises(ValidationError):
            summary.count = 0


class TestUserSessionLiveness:
    """UserSession.is_live — heartbeat-freshness liveness check (biff-mue)."""

    def test_fresh_session_is_live(self) -> None:
        now = datetime.now(UTC)
        session = UserSession(user="kai", last_active=now)
        assert session.is_live(now=now, ttl_seconds=120.0) is True

    def test_within_window_is_live(self) -> None:
        now = datetime.now(UTC)
        session = UserSession(user="kai", last_active=now - timedelta(seconds=90))
        assert session.is_live(now=now, ttl_seconds=120.0) is True

    def test_beyond_window_is_not_live(self) -> None:
        now = datetime.now(UTC)
        session = UserSession(user="kai", last_active=now - timedelta(seconds=200))
        assert session.is_live(now=now, ttl_seconds=120.0) is False

    def test_hours_old_is_not_live(self) -> None:
        now = datetime.now(UTC)
        session = UserSession(user="kai", last_active=now - timedelta(hours=5))
        assert session.is_live(now=now, ttl_seconds=120.0) is False
