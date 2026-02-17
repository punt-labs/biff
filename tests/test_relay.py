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
        msg = Message(from_user="kai", to_user="eric:tty2", body="hello")
        await relay.deliver(msg)
        assert (nested / "inbox-eric-tty2.jsonl").exists()

    async def test_deliver_single(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="hello")
        await relay.deliver(msg)
        messages = relay._read_inbox("eric:tty2")
        assert len(messages) == 1
        assert messages[0].body == "hello"

    async def test_deliver_multiple(self, relay: LocalRelay) -> None:
        for i in range(3):
            await relay.deliver(
                Message(from_user="kai", to_user="eric:tty2", body=f"msg {i}")
            )
        assert len(relay._read_inbox("eric:tty2")) == 3

    async def test_preserves_all_fields(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="auth ready")
        await relay.deliver(msg)
        restored = relay._read_inbox("eric:tty2")[0]
        assert restored.id == msg.id
        assert restored.from_user == msg.from_user
        assert restored.to_user == msg.to_user
        assert restored.body == msg.body
        assert restored.timestamp == msg.timestamp
        assert restored.read == msg.read

    async def test_per_session_inbox_files(self, relay: LocalRelay) -> None:
        await relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="for eric")
        )
        await relay.deliver(
            Message(from_user="kai", to_user="jess:tty3", body="for jess")
        )
        assert len(relay._read_inbox("eric:tty2")) == 1
        assert len(relay._read_inbox("jess:tty3")) == 1
        assert relay._read_inbox("eric:tty2")[0].body == "for eric"

    async def test_broadcast_delivers_to_all_sessions(self, relay: LocalRelay) -> None:
        """Broadcast (bare user) delivers to every registered session."""
        await relay.update_session(UserSession(user="eric", tty="tty2"))
        await relay.update_session(UserSession(user="eric", tty="tty9"))
        await relay.deliver(Message(from_user="kai", to_user="eric", body="hi all"))
        assert len(relay._read_inbox("eric:tty2")) == 1
        assert len(relay._read_inbox("eric:tty9")) == 1

    async def test_broadcast_fallback_when_no_sessions(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        """Broadcast with no registered sessions writes to bare-user inbox."""
        await relay.deliver(Message(from_user="kai", to_user="eric", body="hello"))
        assert (tmp_path / "inbox-eric.jsonl").exists()


# -- Fetch --


class TestFetch:
    async def test_empty(self, relay: LocalRelay) -> None:
        assert await relay.fetch("eric:tty2") == []

    async def test_returns_unread_only(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="old")
        await relay.deliver(msg)
        await relay.mark_read("eric:tty2", [msg.id])
        await relay.deliver(Message(from_user="kai", to_user="eric:tty2", body="new"))
        unread = await relay.fetch("eric:tty2")
        assert len(unread) == 1
        assert unread[0].body == "new"

    async def test_oldest_first(self, relay: LocalRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric:tty2", body="first"))
        await relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="second")
        )
        unread = await relay.fetch("eric:tty2")
        assert unread[0].body == "first"
        assert unread[1].body == "second"

    async def test_isolated_per_session(self, relay: LocalRelay) -> None:
        await relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="for eric")
        )
        await relay.deliver(
            Message(from_user="kai", to_user="jess:tty3", body="for jess")
        )
        assert len(await relay.fetch("eric:tty2")) == 1
        assert len(await relay.fetch("jess:tty3")) == 1


# -- Mark Read --


class TestMarkRead:
    async def test_marks_specific_messages(self, relay: LocalRelay) -> None:
        m1 = Message(from_user="kai", to_user="eric:tty2", body="one")
        m2 = Message(from_user="kai", to_user="eric:tty2", body="two")
        await relay.deliver(m1)
        await relay.deliver(m2)
        await relay.mark_read("eric:tty2", [m1.id])
        messages = relay._read_inbox("eric:tty2")
        assert messages[0].read is True
        assert messages[1].read is False

    async def test_idempotent(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="hello")
        await relay.deliver(msg)
        await relay.mark_read("eric:tty2", [msg.id])
        await relay.mark_read("eric:tty2", [msg.id])
        assert relay._read_inbox("eric:tty2")[0].read is True

    async def test_preserves_other_fields(self, relay: LocalRelay) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="hello")
        await relay.deliver(msg)
        await relay.mark_read("eric:tty2", [msg.id])
        updated = relay._read_inbox("eric:tty2")[0]
        assert updated.id == msg.id
        assert updated.from_user == msg.from_user
        assert updated.body == msg.body
        assert updated.timestamp == msg.timestamp


# -- Unread Summary --


class TestGetUnreadSummary:
    async def test_empty(self, relay: LocalRelay) -> None:
        summary = await relay.get_unread_summary("eric:tty2")
        assert summary.count == 0
        assert summary.preview == ""

    async def test_single_message(self, relay: LocalRelay) -> None:
        await relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="auth ready")
        )
        summary = await relay.get_unread_summary("eric:tty2")
        assert summary.count == 1
        assert "@kai" in summary.preview
        assert "auth ready" in summary.preview

    async def test_multiple_messages(self, relay: LocalRelay) -> None:
        await relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="auth ready")
        )
        await relay.deliver(
            Message(from_user="jess", to_user="eric:tty2", body="tests pass")
        )
        summary = await relay.get_unread_summary("eric:tty2")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview

    async def test_preview_truncated(self, relay: LocalRelay) -> None:
        for i in range(5):
            await relay.deliver(
                Message(
                    from_user=f"user{i}",
                    to_user="eric:tty2",
                    body="a very long message body that goes on and on",
                )
            )
        summary = await relay.get_unread_summary("eric:tty2")
        assert summary.count == 5
        assert len(summary.preview) <= 80


# -- Sessions --


class TestUpdateSession:
    async def test_creates_data_dir(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b"
        relay = LocalRelay(data_dir=nested)
        session = UserSession(user="kai", tty="tty1", plan="coding")
        await relay.update_session(session)
        assert (nested / "sessions.json").exists()

    async def test_create_new_session(self, relay: LocalRelay) -> None:
        session = UserSession(user="kai", tty="tty1", plan="refactoring auth")
        await relay.update_session(session)
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.plan == "refactoring auth"

    async def test_update_existing_session(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="old plan"))
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="new plan"))
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.plan == "new plan"

    async def test_preserves_other_sessions(self, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="tty1", plan="kai's plan")
        )
        await relay.update_session(
            UserSession(user="eric", tty="tty2", plan="eric's plan")
        )
        kai = await relay.get_session("kai:tty1")
        eric = await relay.get_session("eric:tty2")
        assert kai is not None and kai.plan == "kai's plan"
        assert eric is not None and eric.plan == "eric's plan"


class TestGetSession:
    async def test_missing_user(self, relay: LocalRelay) -> None:
        assert await relay.get_session("nobody:tty0") is None

    async def test_returns_full_session(self, relay: LocalRelay) -> None:
        session = UserSession(
            user="kai", tty="tty1", plan="testing", biff_enabled=False
        )
        await relay.update_session(session)
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.user == "kai"
        assert result.tty == "tty1"
        assert result.plan == "testing"
        assert result.biff_enabled is False


class TestGetSessionsForUser:
    async def test_empty(self, relay: LocalRelay) -> None:
        assert await relay.get_sessions_for_user("nobody") == []

    async def test_returns_all_sessions_for_user(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="coding"))
        await relay.update_session(UserSession(user="kai", tty="tty9", plan="testing"))
        await relay.update_session(UserSession(user="eric", tty="tty2", plan="other"))
        sessions = await relay.get_sessions_for_user("kai")
        assert len(sessions) == 2
        ttys = {s.tty for s in sessions}
        assert ttys == {"tty1", "tty9"}

    async def test_does_not_include_other_users(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        await relay.update_session(UserSession(user="eric", tty="tty2"))
        sessions = await relay.get_sessions_for_user("kai")
        assert all(s.user == "kai" for s in sessions)


class TestGetSessions:
    async def test_empty(self, relay: LocalRelay) -> None:
        assert await relay.get_sessions() == []

    async def test_returns_all_sessions(self, relay: LocalRelay) -> None:
        now = datetime.now(UTC)
        recent = UserSession(user="kai", tty="tty1", last_active=now)
        old = UserSession(
            user="eric", tty="tty2", last_active=now - timedelta(seconds=300)
        )
        await relay.update_session(recent)
        await relay.update_session(old)
        sessions = await relay.get_sessions()
        users = {s.user for s in sessions}
        assert users == {"kai", "eric"}


class TestDeleteSession:
    async def test_removes_session(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="coding"))
        await relay.delete_session("kai:tty1")
        assert await relay.get_session("kai:tty1") is None

    async def test_preserves_other_sessions(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="coding"))
        await relay.update_session(UserSession(user="eric", tty="tty2", plan="review"))
        await relay.delete_session("kai:tty1")
        assert await relay.get_session("kai:tty1") is None
        assert await relay.get_session("eric:tty2") is not None

    async def test_noop_for_missing_session(self, relay: LocalRelay) -> None:
        """Deleting a nonexistent session is a no-op."""
        await relay.delete_session("nobody:tty0")


class TestRemoveSentinel:
    """Sentinel file mechanism for robust session removal."""

    def test_write_creates_sentinel_file(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        relay.write_remove_sentinel("kai:tty1")
        sentinel = tmp_path / "remove-kai-tty1"
        assert sentinel.exists()
        assert sentinel.read_text() == "kai:tty1"

    async def test_reap_deletes_session(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1", plan="coding"))
        relay.write_remove_sentinel("kai:tty1")
        relay.reap_sentinels()
        assert await relay.get_session("kai:tty1") is None

    async def test_reap_removes_sentinel_file(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        relay.write_remove_sentinel("kai:tty1")
        relay.reap_sentinels()
        assert not (tmp_path / "remove-kai-tty1").exists()

    async def test_reap_preserves_other_sessions(self, relay: LocalRelay) -> None:
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        await relay.update_session(UserSession(user="eric", tty="tty2"))
        relay.write_remove_sentinel("kai:tty1")
        relay.reap_sentinels()
        assert await relay.get_session("kai:tty1") is None
        assert await relay.get_session("eric:tty2") is not None

    async def test_get_sessions_reaps_automatically(self, relay: LocalRelay) -> None:
        """get_sessions() processes sentinels before returning."""
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        await relay.update_session(UserSession(user="eric", tty="tty2"))
        relay.write_remove_sentinel("kai:tty1")
        sessions = await relay.get_sessions()
        assert all(s.user != "kai" for s in sessions)

    async def test_get_sessions_for_user_reaps(self, relay: LocalRelay) -> None:
        """get_sessions_for_user() processes sentinels before returning."""
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        await relay.update_session(UserSession(user="kai", tty="tty9"))
        relay.write_remove_sentinel("kai:tty1")
        sessions = await relay.get_sessions_for_user("kai")
        assert len(sessions) == 1
        assert sessions[0].tty == "tty9"

    async def test_reap_survives_concurrent_write(self, relay: LocalRelay) -> None:
        """Sentinel survives even if another server overwrites sessions.json."""
        await relay.update_session(UserSession(user="kai", tty="tty1"))
        await relay.update_session(UserSession(user="eric", tty="tty2"))
        # Signal handler writes sentinel
        relay.write_remove_sentinel("kai:tty1")
        # Simulate another server doing a heartbeat (read-modify-write)
        # that restores kai's session
        await relay.heartbeat("eric:tty2")
        # get_sessions still reaps kai because sentinel file persists
        sessions = await relay.get_sessions()
        assert all(s.user != "kai" for s in sessions)

    def test_reap_noop_when_no_sentinels(self, relay: LocalRelay) -> None:
        relay.reap_sentinels()  # no-op, no crash

    def test_reap_noop_when_no_data_dir(self, tmp_path: Path) -> None:
        relay = LocalRelay(data_dir=tmp_path / "nonexistent")
        relay.reap_sentinels()  # no-op, no crash


class TestHeartbeat:
    async def test_creates_new_session(self, relay: LocalRelay) -> None:
        await relay.heartbeat("kai:tty1")
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.user == "kai"
        assert result.tty == "tty1"
        assert result.plan == ""

    async def test_updates_last_active(self, relay: LocalRelay) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await relay.update_session(
            UserSession(user="kai", tty="tty1", plan="coding", last_active=old_time)
        )
        await relay.heartbeat("kai:tty1")
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.last_active > old_time

    async def test_preserves_plan(self, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="tty1", plan="refactoring")
        )
        await relay.heartbeat("kai:tty1")
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.plan == "refactoring"

    async def test_preserves_biff_enabled(self, relay: LocalRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty="tty1", biff_enabled=False)
        )
        await relay.heartbeat("kai:tty1")
        result = await relay.get_session("kai:tty1")
        assert result is not None
        assert result.biff_enabled is False


# -- Session Key Validation --


class TestSessionKeyValidation:
    async def test_rejects_missing_colon(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid session key"):
            await relay.fetch("eric")

    async def test_rejects_path_traversal_in_user(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("../../etc:tty1")

    async def test_rejects_slash_in_user(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("user/name:tty1")

    async def test_rejects_backslash_in_user(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch("user\\name:tty1")

    async def test_rejects_empty_user(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid username"):
            await relay.fetch(":tty1")

    async def test_rejects_empty_tty(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid tty"):
            await relay.fetch("eric:")

    async def test_rejects_path_traversal_in_tty(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid tty"):
            await relay.fetch("eric:../passwd")

    async def test_allows_valid_session_key(self, relay: LocalRelay) -> None:
        assert await relay.fetch("jmf-pobox:a1b2c3d4") == []

    async def test_heartbeat_validates(self, relay: LocalRelay) -> None:
        with pytest.raises(ValueError, match="Invalid session key"):
            await relay.heartbeat("nocolon")


# -- Malformed Data --


class TestMalformedInbox:
    async def test_skips_malformed_lines(
        self, relay: LocalRelay, tmp_path: Path
    ) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="valid")
        await relay.deliver(msg)
        inbox = tmp_path / "inbox-eric-tty2.jsonl"
        with inbox.open("a") as f:
            f.write("this is not json\n")
        messages = relay._read_inbox("eric:tty2")
        assert len(messages) == 1
        assert messages[0].body == "valid"

    async def test_skips_blank_lines(self, relay: LocalRelay, tmp_path: Path) -> None:
        msg = Message(from_user="kai", to_user="eric:tty2", body="valid")
        await relay.deliver(msg)
        inbox = tmp_path / "inbox-eric-tty2.jsonl"
        with inbox.open("a") as f:
            f.write("\n\n\n")
        assert len(relay._read_inbox("eric:tty2")) == 1

    def test_missing_file_returns_empty(self, relay: LocalRelay) -> None:
        assert relay._read_inbox("nobody:tty0") == []


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
        session = UserSession(user="kai", tty="tty1", plan="testing")
        data = {"kai:tty1": session.model_dump(mode="json")}
        (tmp_path / "sessions.json").write_text(json.dumps(data))
        result = relay._read_sessions()
        assert "kai:tty1" in result
        assert result["kai:tty1"].plan == "testing"
