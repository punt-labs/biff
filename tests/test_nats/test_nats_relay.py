"""Tests for NatsRelay against a real nats-server subprocess.

Mirrors tests/test_relay.py but exercises NATS KV and JetStream
rather than filesystem I/O.  Includes user-inbox tests for the
per-user broadcast mailbox.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from biff.models import Message, UserSession
from biff.nats_relay import NatsRelay

pytestmark = pytest.mark.nats

_KAI_TTY = "tty1"
_ERIC_TTY = "tty2"


# -- Deliver + Fetch --


class TestDeliver:
    async def test_deliver_and_fetch(self, relay: NatsRelay) -> None:
        msg = Message(
            from_user="kai",
            to_user=f"eric:{_ERIC_TTY}",
            body="hello",
        )
        await relay.deliver(msg)
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].body == "hello"

    async def test_deliver_multiple(self, relay: NatsRelay) -> None:
        for i in range(3):
            await relay.deliver(
                Message(
                    from_user="kai",
                    to_user=f"eric:{_ERIC_TTY}",
                    body=f"msg {i}",
                )
            )
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 3

    async def test_preserves_all_fields(self, relay: NatsRelay) -> None:
        msg = Message(
            from_user="kai",
            to_user=f"eric:{_ERIC_TTY}",
            body="auth ready",
        )
        await relay.deliver(msg)
        restored = (await relay.fetch(f"eric:{_ERIC_TTY}"))[0]
        assert restored.id == msg.id
        assert restored.from_user == msg.from_user
        assert restored.to_user == msg.to_user
        assert restored.body == msg.body
        assert restored.timestamp == msg.timestamp

    async def test_per_user_isolation(self, relay: NatsRelay) -> None:
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="for eric",
            )
        )
        await relay.deliver(
            Message(
                from_user="kai",
                to_user="jess:tty3",
                body="for jess",
            )
        )
        assert len(await relay.fetch(f"eric:{_ERIC_TTY}")) == 1
        assert len(await relay.fetch("jess:tty3")) == 1


# -- Fetch (POP semantics) --


class TestFetch:
    async def test_empty(self, relay: NatsRelay) -> None:
        assert await relay.fetch(f"eric:{_ERIC_TTY}") == []

    async def test_consumed_on_fetch(self, relay: NatsRelay) -> None:
        """WORK_QUEUE: messages are deleted after ack (fetch)."""
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="once",
            )
        )
        first = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(first) == 1
        # Second fetch should be empty — messages consumed
        second = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert second == []

    async def test_oldest_first(self, relay: NatsRelay) -> None:
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="first",
            )
        )
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="second",
            )
        )
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert unread[0].body == "first"
        assert unread[1].body == "second"


# -- Mark Read (no-op) --


class TestMarkRead:
    async def test_is_noop(self, relay: NatsRelay) -> None:
        """mark_read is a no-op — fetch already consumed the messages."""
        import uuid

        await relay.mark_read(f"eric:{_ERIC_TTY}", [uuid.uuid4()])
        # No error, no effect


# -- Unread Summary --


class TestGetUnreadSummary:
    async def test_empty(self, relay: NatsRelay) -> None:
        summary = await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        assert summary.count == 0
        assert summary.preview == ""

    async def test_single_message(self, relay: NatsRelay) -> None:
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="auth ready",
            )
        )
        summary = await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        assert summary.count == 1
        assert "@kai" in summary.preview
        assert "auth ready" in summary.preview

    async def test_multiple_messages(self, relay: NatsRelay) -> None:
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="auth ready",
            )
        )
        await relay.deliver(
            Message(
                from_user="jess",
                to_user=f"eric:{_ERIC_TTY}",
                body="tests pass",
            )
        )
        summary = await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview

    async def test_non_destructive(self, relay: NatsRelay) -> None:
        """Summary should not consume messages."""
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="still here",
            )
        )
        await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        # Messages should still be fetchable
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].body == "still here"

    async def test_preview_truncated(self, relay: NatsRelay) -> None:
        for i in range(5):
            await relay.deliver(
                Message(
                    from_user=f"user{i}",
                    to_user=f"eric:{_ERIC_TTY}",
                    body="a very long message body that goes on and on",
                )
            )
        summary = await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        assert summary.count == 5
        assert len(summary.preview) <= 80

    async def test_merges_tty_and_user_inboxes(self, relay: NatsRelay) -> None:
        """Unread summary includes messages from both inboxes."""
        await relay.deliver(
            Message(from_user="kai", to_user=f"eric:{_ERIC_TTY}", body="targeted msg")
        )
        await relay.deliver(
            Message(from_user="jess", to_user="eric", body="broadcast msg")
        )
        summary = await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        assert summary.count == 2
        assert "@kai" in summary.preview
        assert "@jess" in summary.preview


# -- User Inbox --


class TestUserInbox:
    async def test_deliver_and_fetch(self, relay: NatsRelay) -> None:
        msg = Message(from_user="kai", to_user="eric", body="broadcast hello")
        await relay.deliver(msg)
        unread = await relay.fetch_user_inbox("eric")
        assert len(unread) == 1
        assert unread[0].body == "broadcast hello"

    async def test_pop_semantics(self, relay: NatsRelay) -> None:
        """Messages are consumed on fetch — second fetch is empty."""
        await relay.deliver(Message(from_user="kai", to_user="eric", body="once"))
        first = await relay.fetch_user_inbox("eric")
        assert len(first) == 1
        second = await relay.fetch_user_inbox("eric")
        assert second == []

    async def test_persists_offline(self, relay: NatsRelay) -> None:
        """Broadcast delivers even with no active sessions."""
        # No sessions registered for eric
        await relay.deliver(
            Message(from_user="kai", to_user="eric", body="offline msg")
        )
        unread = await relay.fetch_user_inbox("eric")
        assert len(unread) == 1
        assert unread[0].body == "offline msg"

    async def test_count(self, relay: NatsRelay) -> None:
        assert await relay.get_user_unread_count("eric") == 0
        await relay.deliver(Message(from_user="kai", to_user="eric", body="a"))
        await relay.deliver(Message(from_user="kai", to_user="eric", body="b"))
        assert await relay.get_user_unread_count("eric") == 2

    async def test_does_not_consume_tty_messages(self, relay: NatsRelay) -> None:
        """User inbox fetch does not consume TTY inbox messages."""
        await relay.deliver(
            Message(from_user="kai", to_user=f"eric:{_ERIC_TTY}", body="targeted")
        )
        await relay.deliver(Message(from_user="kai", to_user="eric", body="broadcast"))
        # Fetch user inbox — should only get broadcast
        user_msgs = await relay.fetch_user_inbox("eric")
        assert len(user_msgs) == 1
        assert user_msgs[0].body == "broadcast"
        # TTY inbox still has the targeted message
        tty_msgs = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(tty_msgs) == 1
        assert tty_msgs[0].body == "targeted"


# -- Sessions --


class TestUpdateSession:
    async def test_create_new_session(self, relay: NatsRelay) -> None:
        session = UserSession(user="kai", tty=_KAI_TTY, plan="refactoring auth")
        await relay.update_session(session)
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.plan == "refactoring auth"

    async def test_update_existing_session(self, relay: NatsRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="old plan")
        )
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="new plan")
        )
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.plan == "new plan"

    async def test_preserves_other_sessions(self, relay: NatsRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="kai's plan")
        )
        await relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="eric's plan")
        )
        kai = await relay.get_session(f"kai:{_KAI_TTY}")
        eric = await relay.get_session(f"eric:{_ERIC_TTY}")
        assert kai is not None and kai.plan == "kai's plan"
        assert eric is not None and eric.plan == "eric's plan"


class TestGetSession:
    async def test_missing_user(self, relay: NatsRelay) -> None:
        assert await relay.get_session("nobody:tty0") is None

    async def test_returns_full_session(self, relay: NatsRelay) -> None:
        session = UserSession(
            user="kai",
            tty=_KAI_TTY,
            plan="testing",
            biff_enabled=False,
        )
        await relay.update_session(session)
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == "testing"
        assert result.biff_enabled is False


class TestGetSessions:
    async def test_empty(self, relay: NatsRelay) -> None:
        assert await relay.get_sessions() == []

    async def test_returns_all_sessions(self, relay: NatsRelay) -> None:
        now = datetime.now(UTC)
        recent = UserSession(user="kai", tty=_KAI_TTY, last_active=now)
        old = UserSession(
            user="eric",
            tty=_ERIC_TTY,
            last_active=now - timedelta(seconds=300),
        )
        await relay.update_session(recent)
        await relay.update_session(old)
        sessions = await relay.get_sessions()
        users = {s.user for s in sessions}
        assert users == {"kai", "eric"}


class TestHeartbeat:
    async def test_creates_new_session(self, relay: NatsRelay) -> None:
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.user == "kai"
        assert result.plan == ""

    async def test_updates_last_active(self, relay: NatsRelay) -> None:
        old_time = datetime.now(UTC) - timedelta(seconds=300)
        await relay.update_session(
            UserSession(
                user="kai",
                tty=_KAI_TTY,
                plan="coding",
                last_active=old_time,
            )
        )
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.last_active > old_time

    async def test_preserves_plan(self, relay: NatsRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, plan="refactoring")
        )
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.plan == "refactoring"

    async def test_preserves_biff_enabled(self, relay: NatsRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, biff_enabled=False)
        )
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.biff_enabled is False


# -- Cross-relay (simulates two MCP servers) --


class TestCrossRelay:
    """Two NatsRelay instances sharing the same nats-server."""

    async def test_session_visible_across_relays(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await relay.update_session(UserSession(user="kai", tty=_KAI_TTY, plan="coding"))
        result = await second_relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.plan == "coding"

    async def test_message_delivery_across_relays(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        msg = Message(
            from_user="kai",
            to_user=f"eric:{_ERIC_TTY}",
            body="PR ready",
        )
        await relay.deliver(msg)
        unread = await second_relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].body == "PR ready"

    async def test_who_sees_both(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await relay.update_session(UserSession(user="kai", tty=_KAI_TTY, plan="coding"))
        await second_relay.update_session(
            UserSession(user="eric", tty=_ERIC_TTY, plan="reviewing")
        )
        sessions = await relay.get_sessions()
        users = {s.user for s in sessions}
        assert "kai" in users
        assert "eric" in users
