"""Tests for NatsRelay against a real nats-server subprocess.

Mirrors tests/test_relay.py but exercises NATS KV and JetStream
rather than filesystem I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from biff.models import Message, UserSession
from biff.nats_relay import NatsRelay

pytestmark = pytest.mark.nats


# -- Deliver + Fetch --


class TestDeliver:
    async def test_deliver_and_fetch(self, relay: NatsRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="hello")
        await relay.deliver(msg)
        unread = await relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].body == "hello"

    async def test_deliver_multiple(self, relay: NatsRelay) -> None:
        for i in range(3):
            await relay.deliver(
                Message(from_user="kai", to_user="eric", body=f"msg {i}")
            )
        unread = await relay.fetch("eric")
        assert len(unread) == 3

    async def test_preserves_all_fields(self, relay: NatsRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="auth ready")
        await relay.deliver(msg)
        restored = (await relay.fetch("eric"))[0]
        assert restored.id == msg.id
        assert restored.from_user == msg.from_user
        assert restored.to_user == msg.to_user
        assert restored.body == msg.body
        assert restored.timestamp == msg.timestamp

    async def test_per_user_isolation(self, relay: NatsRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="for eric"))
        await relay.deliver(Message(from_user="kai", to_user="jess", body="for jess"))
        assert len(await relay.fetch("eric")) == 1
        assert len(await relay.fetch("jess")) == 1


# -- Fetch (POP semantics) --


class TestFetch:
    async def test_empty(self, relay: NatsRelay) -> None:
        assert await relay.fetch("eric") == []

    async def test_consumed_on_fetch(self, relay: NatsRelay) -> None:
        """WORK_QUEUE: messages are deleted after ack (fetch)."""
        await relay.deliver(Message(from_user="kai", to_user="eric", body="once"))
        first = await relay.fetch("eric")
        assert len(first) == 1
        # Second fetch should be empty — messages consumed
        second = await relay.fetch("eric")
        assert second == []

    async def test_oldest_first(self, relay: NatsRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="first"))
        await relay.deliver(Message(from_user="kai", to_user="eric", body="second"))
        unread = await relay.fetch("eric")
        assert unread[0].body == "first"
        assert unread[1].body == "second"


# -- Mark Read (no-op) --


class TestMarkRead:
    async def test_is_noop(self, relay: NatsRelay) -> None:
        """mark_read is a no-op — fetch already consumed the messages."""
        import uuid

        await relay.mark_read("eric", [uuid.uuid4()])
        # No error, no effect


# -- Unread Summary --


class TestGetUnreadSummary:
    async def test_empty(self, relay: NatsRelay) -> None:
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 0
        assert summary.preview == ""

    async def test_single_message(self, relay: NatsRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="auth ready"))
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 1
        assert "@kai" in summary.preview
        assert "auth ready" in summary.preview

    async def test_multiple_messages(self, relay: NatsRelay) -> None:
        await relay.deliver(Message(from_user="kai", to_user="eric", body="auth ready"))
        await relay.deliver(
            Message(from_user="jess", to_user="eric", body="tests pass")
        )
        summary = await relay.get_unread_summary("eric")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview

    async def test_non_destructive(self, relay: NatsRelay) -> None:
        """Summary should not consume messages."""
        await relay.deliver(Message(from_user="kai", to_user="eric", body="still here"))
        await relay.get_unread_summary("eric")
        # Messages should still be fetchable
        unread = await relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].body == "still here"

    async def test_preview_truncated(self, relay: NatsRelay) -> None:
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
    async def test_create_new_session(self, relay: NatsRelay) -> None:
        session = UserSession(user="kai", plan="refactoring auth")
        await relay.update_session(session)
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "refactoring auth"

    async def test_update_existing_session(self, relay: NatsRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="old plan"))
        await relay.update_session(UserSession(user="kai", plan="new plan"))
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "new plan"

    async def test_preserves_other_sessions(self, relay: NatsRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="kai's plan"))
        await relay.update_session(UserSession(user="eric", plan="eric's plan"))
        kai = await relay.get_session("kai")
        eric = await relay.get_session("eric")
        assert kai is not None and kai.plan == "kai's plan"
        assert eric is not None and eric.plan == "eric's plan"


class TestGetSession:
    async def test_missing_user(self, relay: NatsRelay) -> None:
        assert await relay.get_session("nobody") is None

    async def test_returns_full_session(self, relay: NatsRelay) -> None:
        session = UserSession(user="kai", plan="testing", biff_enabled=False)
        await relay.update_session(session)
        result = await relay.get_session("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == "testing"
        assert result.biff_enabled is False


class TestGetActiveSessions:
    async def test_empty(self, relay: NatsRelay) -> None:
        assert await relay.get_active_sessions() == []

    async def test_filters_by_ttl(self, relay: NatsRelay) -> None:
        now = datetime.now(UTC)
        recent = UserSession(user="kai", last_active=now)
        stale = UserSession(user="eric", last_active=now - timedelta(seconds=300))
        await relay.update_session(recent)
        await relay.update_session(stale)
        active = await relay.get_active_sessions(ttl=120)
        assert len(active) == 1
        assert active[0].user == "kai"

    async def test_custom_ttl(self, relay: NatsRelay) -> None:
        now = datetime.now(UTC)
        session = UserSession(user="kai", last_active=now - timedelta(seconds=60))
        await relay.update_session(session)
        assert len(await relay.get_active_sessions(ttl=30)) == 0
        assert len(await relay.get_active_sessions(ttl=120)) == 1


class TestHeartbeat:
    async def test_creates_new_session(self, relay: NatsRelay) -> None:
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == ""

    async def test_updates_last_active(self, relay: NatsRelay) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await relay.update_session(
            UserSession(user="kai", plan="coding", last_active=old_time)
        )
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.last_active > old_time

    async def test_preserves_plan(self, relay: NatsRelay) -> None:
        await relay.update_session(UserSession(user="kai", plan="refactoring"))
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.plan == "refactoring"

    async def test_preserves_biff_enabled(self, relay: NatsRelay) -> None:
        await relay.update_session(UserSession(user="kai", biff_enabled=False))
        await relay.heartbeat("kai")
        result = await relay.get_session("kai")
        assert result is not None
        assert result.biff_enabled is False


# -- Cross-relay (simulates two MCP servers) --


class TestCrossRelay:
    """Two NatsRelay instances sharing the same nats-server."""

    async def test_session_visible_across_relays(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await relay.update_session(UserSession(user="kai", plan="coding"))
        result = await second_relay.get_session("kai")
        assert result is not None
        assert result.plan == "coding"

    async def test_message_delivery_across_relays(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        msg = Message(from_user="kai", to_user="eric", body="PR ready")
        await relay.deliver(msg)
        unread = await second_relay.fetch("eric")
        assert len(unread) == 1
        assert unread[0].body == "PR ready"

    async def test_who_sees_both(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await relay.update_session(UserSession(user="kai", plan="coding"))
        await second_relay.update_session(UserSession(user="eric", plan="reviewing"))
        active = await relay.get_active_sessions()
        users = {s.user for s in active}
        assert "kai" in users
        assert "eric" in users
