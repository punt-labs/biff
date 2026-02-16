"""Tests for the local filesystem relay."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from biff.models import Message, UserSession
from biff.relay import LocalRelay


@pytest.fixture
def relay(tmp_path: Path) -> LocalRelay:
    return LocalRelay(data_dir=tmp_path)


# -- Deliver --


class TestDeliver:
    async def test_creates_data_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        relay = LocalRelay(data_dir=nested)
        msg = Message(from_user="kai", to_user="eric", body="hello")
        await relay.deliver(msg)
        assert (nested / "inbox-eric.jsonl").exists()

    async def test_deliver_single(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        await relay.deliver(msg)
        messages = relay._read_inbox("eric")
        assert len(messages) == 1
        assert messages[0].body == "hello"

    async def test_deliver_multiple(self, relay: LocalRelay) -> None:
        for i in range(3):
            await relay.deliver(
                Message(from_user="kai", to_user="eric", body=f"msg {i}")
            )
        assert len(relay._read_inbox("eric")) == 3

    async def test_preserves_all_fields(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="auth ready")
        await relay.deliver(msg)
        restored = relay._read_inbox("eric")[0]
        assert restored.id == msg.id
        assert restored.from_user == msg.from_user
        assert restored.to_user == msg.to_user
        assert restored.body == msg.body
        assert restored.timestamp == msg.timestamp
        assert restored.read == msg.read

    async def test_per_user_inbox_files(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="for eric"))
        await relay.deliver(Message(from_user="kai", to_user="jess", body="for jess"))
        assert len(relay._read_inbox("eric")) == 1
        assert len(relay._read_inbox("jess")) == 1
        assert relay._read_inbox("eric")[0].body == "for eric"


# -- Fetch --


class TestFetch:
    async def test_empty(self, relay: LocalRelay) -> None:
        assert await relay.fetch("eric") == []

    async def test_returns_unread_only(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="old")
        await relay.deliver(msg)
        await relay.mark_read("eric", [msg.id])
        await relay.deliver(Message(from_user="kai", to_user="eric", body="new"))
        unread = await relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].body == "new"

    async def test_oldest_first(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="first"))
        await relay.deliver(Message(from_user="kai", to_user="eric", body="second"))
        unread = await relay.fetch("eric")
        assert unread[0].body == "first"
        assert unread[1].body == "second"

    async def test_isolated_per_user(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="for eric"))
        await relay.deliver(Message(from_user="kai", to_user="jess", body="for jess"))
        assert len(await relay.fetch("eric")) == 1
        assert len(await relay.fetch("jess")) == 1


# -- Mark Read --


class TestMarkRead:
    async def test_marks_specific_messages(self, relay: LocalRelay) -> None:
        m1 = Message(from_user="kai", to_user="eric", body="one")
        m2 = Message(from_user="kai", to_user="eric", body="two")
        await relay.deliver(m1)
        await relay.deliver(m2)
        await relay.mark_read("eric", [m1.id])
        messages = relay._read_inbox("eric")
        assert messages[0].read is True
        assert messages[1].read is False

    async def test_idempotent(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        await relay.deliver(msg)
        await relay.mark_read("eric", [msg.id])
        await relay.mark_read("eric", [msg.id])
        assert relay._read_inbox("eric")[0].read is True

    async def test_preserves_other_fields(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        await relay.deliver(msg)
        await relay.mark_read("eric", [msg.id])
        updated = relay._read_inbox("eric")[0]
        assert updated.id == msg.id
        assert updated.from_user == msg.from_user
        assert updated.body == msg.body
        assert updated.timestamp == msg.timestamp


# -- Unread Summary --


class TestGetUnreadSummary:
    async def test_empty(self, relay: LocalRelay) -> None:
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 0
        assert summary.preview == ""

    async def test_single_message(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="auth ready"))
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 1
        assert "@kai" in summary.preview
        assert "auth ready" in summary.preview

    async def test_multiple_messages(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="auth ready"))
        await relay.deliver(
            Message(from_user="jess", to_user="eric", body="tests pass")
        )
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview

    async def test_preview_truncated(self, relay: LocalRelay) -> None:
        for i in range(5):
            await relay.deliver(
                Message(
                    from_user=f"user{i}",
                    to_user="eric",
                    body="a very long message body that goes on and on",
                )
            )
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 5
        assert len(summary.preview) <= 80


# -- Sessions --


class TestUpdateSession:
    async def test_creates_data_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        relay = LocalRelay(data_dir=nested)
        session = UserSession(user="kai", plan="coding")
        await relay.update_session(session)
        assert (nested / "sessions.json").exists()

    async def test_create_new_session(self, relay: LocalRelay) -> None:
        session = UserSession(user="kai", plan="refactoring auth")
        await relay.update_session(session)
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "refactoring auth"

    async def test_update_existing_session(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="old plan"))
        await relay.update_session(UserSession(user="kai", plan="new plan"))
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "new plan"

    async def test_preserves_other_sessions(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="kai's plan"))
        await relay.update_session(UserSession(user="eric", plan="eric's plan"))
        kai = await relay.get_session("kai")
        eric = await relay.get_session("eric")
        assert kai is not None and kai.plan == "kai's plan"
        assert eric is not None and eric.plan == "eric's plan"


class TestGetSession:
    async def test_missing_user(self, relay: LocalRelay) -> None:
        assert await relay.get_session("nobody") is None

    async def test_returns_full_session(self, relay: LocalRelay) -> None:
        session = UserSession(user="kai", plan="testing", biff_enabled=False)
        await relay.update_session(session)
        result = await relay.get_session("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == "testing"
        assert result.biff_enabled is False


class TestGetSessions:
    async def test_empty(self, relay: LocalRelay) -> None:
        assert await relay.get_sessions() == []

    async def test_returns_all_sessions(self, relay: LocalRelay) -> None:
        now = datetime.now(UTC)
        recent = UserSession(user="kai", last_active=now)
        old = UserSession(user="eric", last_active=now - timedelta(seconds=300))
        await relay.update_session(recent)
        await relay.update_session(old)
        sessions = await relay.get_sessions()
        users = {s.user for s in sessions}
        assert users == {"kai", "eric"}


class TestHeartbeat:
    async def test_creates_new_session(self, relay: LocalRelay) -> None:
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == ""

    async def test_updates_last_active(self, relay: LocalRelay) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await relay.update_session(
            UserSession(user="kai", plan="coding", last_active=old_time)
        )
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.last_active > old_time

    async def test_preserves_plan(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="refactoring"))
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "refactoring"

    async def test_preserves_biff_enabled(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", biff_enabled=False))
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.biff_enabled is False


# -- Username Validation --


class TestUsernameValidation:
    async def test_rejects_path_traversal(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("../../etc/passwd")

    async def test_rejects_forward_slash(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("user/name")

    async def test_rejects_backslash(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("user\\name")

    async def test_rejects_empty(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("")

    async def test_allows_hyphens_and_underscores(self, relay: LocalRelay) -> None:
        assert await relay.fetch("jmf-pobox") == []
        assert await relay.fetch("user_name") == []


# -- Malformed Data --


class TestMalformedInbox:
    async def test_skips_malformed_lines(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        msg = Message(from_user="kai", to_user="eric", body="valid")
        await relay.deliver(msg)
        inbox = tmp_path / "inbox-eric.jsonl"
        with inbox.open("a") as f:
            f.write("this is not json\n")
        messages = relay._read_inbox("eric")
        assert len(messages) == 1
        assert messages[0].body == "valid"

    async def test_skips_blank_lines(self, relay: LocalRelay, tmp_path: Path) -> None:
        msg = Message(from_user="kai", to_user="eric", body="valid")
        await relay.deliver(msg)
        inbox = tmp_path / "inbox-eric.jsonl"
        with inbox.open("a") as f:
            f.write("\n\n\n")
        assert len(relay._read_inbox("eric")) == 1

    def test_missing_file_returns_empty(self, relay: LocalRelay) -> None:
        assert relay._read_inbox("nobody") == []


class TestMalformedSessions:
    def test_corrupt_json_returns_empty(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        (tmp_path / "sessions.json").write_text("not json at all")
        assert relay._read_sessions() == {}

    def test_non_dict_json_returns_empty(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        (tmp_path / "sessions.json").write_text('["a list", "not a dict"]')
        assert relay._read_sessions() == {}

    def test_missing_file_returns_empty(self, relay: LocalRelay) -> None:
        assert relay._read_sessions() == {}

    def test_valid_json_with_file(self, relay: LocalRelay, tmp_path: Path) -> None:
        session = UserSession(user="kai", plan="testing")
        data = {"kai": session.model_dump(mode="json")}
        (tmp_path / "sessions.json").write_text(json.dumps(data))
        result = relay._read_sessions()
        assert "kai" in result
        assert result["kai"].plan == "testing"
