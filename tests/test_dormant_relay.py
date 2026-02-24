"""Tests for the dormant relay (Null Object pattern)."""

from __future__ import annotations

import uuid

from biff.models import Message, SessionEvent, UserSession, WallPost
from biff.relay import DormantRelay


class TestDormantRelay:
    """DormantRelay satisfies the Relay protocol with safe empty returns."""

    async def test_deliver_is_noop(self) -> None:
        relay = DormantRelay()
        msg = Message(from_user="kai", to_user="eric:tty2", body="hello")
        await relay.deliver(msg)  # should not raise

    async def test_fetch_returns_empty(self) -> None:
        relay = DormantRelay()
        assert await relay.fetch("eric:tty2") == []

    async def test_mark_read_is_noop(self) -> None:
        relay = DormantRelay()
        await relay.mark_read("eric:tty2", [uuid.uuid4()])

    async def test_get_unread_summary_returns_zero(self) -> None:
        relay = DormantRelay()
        summary = await relay.get_unread_summary("eric:tty2")
        assert summary.count == 0

    async def test_fetch_user_inbox_returns_empty(self) -> None:
        relay = DormantRelay()
        assert await relay.fetch_user_inbox("eric") == []

    async def test_mark_read_user_inbox_is_noop(self) -> None:
        relay = DormantRelay()
        await relay.mark_read_user_inbox("eric", [uuid.uuid4()])

    async def test_get_user_unread_count_returns_zero(self) -> None:
        relay = DormantRelay()
        assert await relay.get_user_unread_count("eric") == 0

    async def test_update_session_is_noop(self) -> None:
        relay = DormantRelay()
        session = UserSession(user="kai", tty="tty1")
        await relay.update_session(session)

    async def test_get_session_returns_none(self) -> None:
        relay = DormantRelay()
        assert await relay.get_session("kai:tty1") is None

    async def test_get_sessions_for_user_returns_empty(self) -> None:
        relay = DormantRelay()
        assert await relay.get_sessions_for_user("kai") == []

    async def test_heartbeat_is_noop(self) -> None:
        relay = DormantRelay()
        await relay.heartbeat("kai:tty1")

    async def test_get_sessions_returns_empty(self) -> None:
        relay = DormantRelay()
        assert await relay.get_sessions() == []

    async def test_delete_session_is_noop(self) -> None:
        relay = DormantRelay()
        await relay.delete_session("kai:tty1")

    async def test_append_wtmp_is_noop(self) -> None:
        relay = DormantRelay()
        event = SessionEvent(
            session_key="kai:tty1", event="login", user="kai", tty="tty1"
        )
        await relay.append_wtmp(event)

    async def test_get_wtmp_returns_empty(self) -> None:
        relay = DormantRelay()
        assert await relay.get_wtmp() == []
        assert await relay.get_wtmp(user="kai", count=10) == []

    async def test_set_wall_is_noop(self) -> None:
        relay = DormantRelay()
        from datetime import UTC, datetime, timedelta

        wall = WallPost(
            text="hello",
            from_user="kai",
            posted_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        await relay.set_wall(wall)
        await relay.set_wall(None)

    async def test_get_wall_returns_none(self) -> None:
        relay = DormantRelay()
        assert await relay.get_wall() is None

    async def test_close_is_noop(self) -> None:
        relay = DormantRelay()
        await relay.close()
