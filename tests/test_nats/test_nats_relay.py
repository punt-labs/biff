"""Tests for NatsRelay against a real nats-server subprocess.

Mirrors tests/test_relay.py but exercises NATS KV and JetStream
rather than filesystem I/O.  Includes user-inbox tests for the
per-user broadcast mailbox.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from unittest.mock import AsyncMock, MagicMock

import pytest

import biff.nats_relay as nats_relay_module
from biff.models import Message, UserSession, WallPost
from biff.nats_relay import NatsRelay

if TYPE_CHECKING:
    from nats.aio.client import Client as NatsClient
    from nats.js.client import JetStreamContext
    from nats.js.kv import KeyValue

pytestmark = pytest.mark.nats_local

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

    async def test_redelivering_same_message_id_is_deduplicated(
        self, relay: NatsRelay
    ) -> None:
        """Review finding: a retry that reuses the same Message
        (same id) must not create a second copy if the original publish
        actually landed and only its ack was lost. deliver() sets
        Nats-Msg-Id to message.id, so JetStream's own dedup window catches
        this — verified here against a real server, since a mock cannot
        prove server-side dedup behavior.
        """
        msg = Message(
            from_user="kai",
            to_user=f"eric:{_ERIC_TTY}",
            body="only once",
        )
        await relay.deliver(msg)
        await relay.deliver(msg)  # same instance, same id — simulates a retry
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1

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

    async def test_non_destructive(self, relay: NatsRelay) -> None:
        """Summary does not consume messages — uses stream_info only."""
        await relay.deliver(
            Message(
                from_user="kai",
                to_user=f"eric:{_ERIC_TTY}",
                body="still here",
            )
        )
        await relay.get_unread_summary(f"eric:{_ERIC_TTY}")
        unread = await relay.fetch(f"eric:{_ERIC_TTY}")
        assert len(unread) == 1
        assert unread[0].body == "still here"

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
    async def test_skips_missing_session(self, relay: NatsRelay) -> None:
        """Heartbeat is a no-op when no session exists in KV.

        Creating a bare session would destroy tty_name, plan, hostname,
        and other fields that only the lifespan or tool handlers set.
        """
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is None

    async def test_warns_once_on_missing_session(
        self, relay: NatsRelay, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A session vanishing under a live heartbeat loop is anomalous.

        The loop runs on a fixed interval for the life of the process, so
        the warning must fire once per key, not on every tick.
        """
        key = f"kai:{_KAI_TTY}"
        with caplog.at_level("WARNING"):
            await relay.heartbeat(key)
            await relay.heartbeat(key)
            await relay.heartbeat(key)
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert len(warnings) == 1
        assert key in warnings[0].message

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

    async def test_preserves_tty_name(self, relay: NatsRelay) -> None:
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, tty_name="dev-laptop")
        )
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.tty_name == "dev-laptop"

    async def test_does_not_advance_last_tool_at(self, relay: NatsRelay) -> None:
        """Regression: heartbeat must not touch last_tool_at.

        last_tool_at is the idle time /who and /finger display; only a real
        tool invocation (update_current_session) may advance it. Heartbeat
        runs unconditionally on a fixed interval and must leave it alone —
        it may bump last_active (liveness) as always.
        """
        old_tool_at = datetime.now(UTC) - timedelta(minutes=10)
        await relay.update_session(
            UserSession(user="kai", tty=_KAI_TTY, last_tool_at=old_tool_at)
        )
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        await relay.heartbeat(f"kai:{_KAI_TTY}")
        result = await relay.get_session(f"kai:{_KAI_TTY}")
        assert result is not None
        assert result.last_tool_at == old_tool_at
        assert result.last_active > old_tool_at


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


class TestPublishRebuildRobustness:
    """biff-1f2: notification/poke publishes and the wall expiry delete
    reconnect after a client rebuild instead of silently no-opping on a
    handle bound to the discarded client.

    ``_force_reconnect``/``_on_closed`` clear ``self._nc`` (and, on a fresh
    dial, ``self._js``/``self._kv``/``self._names_kv`` alongside it) so the
    next relay call redials.  Before biff-1f2, the two wake-poke publishes
    read ``self._nc`` directly and bailed out on ``None`` instead of
    triggering that redial themselves.
    """

    @staticmethod
    async def _rebuild(relay: NatsRelay) -> None:
        """Simulate the client-discard side of a wedge teardown/give-up close."""
        nc = await relay.get_nc()
        await nc.close()
        # Let biff's own closed_cb (_on_closed) clear _nc/_js/_kv/_names_kv.
        for _ in range(50):
            if relay._nc is None:
                break
            await asyncio.sleep(0.05)
        assert relay._nc is None  # precondition: the rebuild actually happened

    async def test_talk_notification_publishes_after_rebuild(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await self._rebuild(relay)

        received: list[bytes] = []

        async def _cb(msg: object) -> None:
            received.append(getattr(msg, "data", b""))

        nc2 = await second_relay.get_nc()
        subject = relay.talk_notify_subject(f"eric:{_ERIC_TTY}")
        await nc2.subscribe(subject, cb=_cb)  # pyright: ignore[reportUnknownMemberType]
        await asyncio.sleep(0.2)

        msg = Message(
            from_user="kai", to_user=f"eric:{_ERIC_TTY}", body="after rebuild"
        )
        await relay._publish_talk_notification(f"eric:{_ERIC_TTY}", msg, "kai:tty1")
        await asyncio.sleep(0.5)

        assert len(received) == 1
        assert relay._nc is not None  # reconnected, not left discarded

    async def test_inbox_notification_publishes_after_rebuild(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        await self._rebuild(relay)

        received: list[bytes] = []

        async def _cb(msg: object) -> None:
            received.append(getattr(msg, "data", b""))

        nc2 = await second_relay.get_nc()
        subject = relay.inbox_notify_subject(relay._repo_name, "eric")
        await nc2.subscribe(subject, cb=_cb)  # pyright: ignore[reportUnknownMemberType]
        await asyncio.sleep(0.2)

        await relay._publish_inbox_notification(relay._repo_name, "eric")
        await asyncio.sleep(0.5)

        assert len(received) == 1
        assert relay._nc is not None

    async def test_broadcast_deliver_pokes_after_rebuild(
        self, relay: NatsRelay, second_relay: NatsRelay
    ) -> None:
        """The end-to-end path: deliver()'s broadcast branch still pokes."""
        await self._rebuild(relay)

        received: list[bytes] = []

        async def _cb(msg: object) -> None:
            received.append(getattr(msg, "data", b""))

        nc2 = await second_relay.get_nc()
        subject = relay.inbox_notify_subject(relay._repo_name, "eric")
        await nc2.subscribe(subject, cb=_cb)  # pyright: ignore[reportUnknownMemberType]
        await asyncio.sleep(0.2)

        await relay.deliver(Message(from_user="kai", to_user="eric", body="broadcast"))
        await asyncio.sleep(0.5)

        assert len(received) == 1
        unread = await relay.fetch_user_inbox("eric")
        assert len(unread) == 1  # the JetStream delivery itself still landed

    async def test_wall_expiry_delete_uses_a_freshly_resolved_handle(
        self, relay: NatsRelay
    ) -> None:
        """The expiry delete re-resolves the handle instead of reusing the fetch's.

        Proven by counting ``_ensure_connected`` calls: the fix calls it
        once for the ``kv.get`` fetch and once more, immediately before the
        delete — reusing the first call's handle across that ``await`` is
        exactly the staleness bug biff-1f2 closes.
        """
        now = datetime.now(UTC)
        expired = WallPost(
            text="stale",
            from_user="kai",
            posted_at=now - timedelta(hours=2),
            expires_at=now - timedelta(hours=1),
        )
        await relay.set_wall(expired)

        original = relay._ensure_connected
        calls = 0

        async def _counting() -> tuple[JetStreamContext, KeyValue]:
            nonlocal calls
            calls += 1
            return await original()

        relay._ensure_connected = _counting  # type: ignore[method-assign]
        try:
            result = await relay.get_wall()
        finally:
            relay._ensure_connected = original  # type: ignore[method-assign]

        assert result is None
        assert calls == 2  # fetch + a fresh resolve immediately before the delete


class TestLiveNcOrReconnectBounds:
    """The reconnect fall-through inside a best-effort poke stays bounded,
    and a closed relay never resurrects a connection.

    None of these need a live nats-server — all drive a never-connected
    ``NatsRelay`` instance with ``get_nc`` replaced. The durable
    regressions exercise the public surface a caller actually uses
    (``_publish_inbox_notification``, ``deliver()``); the closed-flag
    tests exercise ``_live_nc_or_reconnect`` directly, since the
    terminal-close guard has no further-public surface to observe it
    through.
    """

    async def test_slow_reconnect_does_not_block_a_poke_publish(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The durable regression, against the public surface a caller
        actually uses (not ``_live_nc_or_reconnect`` directly): a hung
        dial is bounded by ``_NOTIFY_RECONNECT_TIMEOUT``, not the much
        larger ``_CONNECT_PROVISION_TIMEOUT`` the unwrapped ``get_nc()``
        fall-through used to be subject to, so the best-effort poke
        publish itself returns promptly and does not raise — mirrors
        the original repro's own surface (``_publish_inbox_notification``,
        not the private helper underneath it).
        """
        monkeypatch.setattr(nats_relay_module, "_NOTIFY_RECONNECT_TIMEOUT", 0.05)

        relay = NatsRelay()
        relay._nc = None  # a wedge teardown discarded the cached client

        async def _hung_get_nc() -> NatsClient:
            await asyncio.sleep(5.0)  # never resolves within the patched bound
            msg = "unreachable — the timeout must fire first"
            raise AssertionError(msg)

        relay.get_nc = _hung_get_nc  # type: ignore[method-assign]

        start = time.monotonic()
        await relay._publish_inbox_notification("repo", "kai")  # must not raise
        elapsed = time.monotonic() - start

        assert elapsed < 1.0  # bounded by the patched 0.05s, not the 5s sleep

    async def test_deliver_succeeds_despite_a_slow_poke_reconnect(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``deliver()``'s durable JetStream publish must not wait on the
        best-effort poke's reconnect: the poke runs after the publish has
        already succeeded and is bounded/best-effort, never on the
        critical path, even when the reconnect it attempts is itself slow.
        """
        monkeypatch.setattr(nats_relay_module, "_NOTIFY_RECONNECT_TIMEOUT", 0.05)

        relay = NatsRelay()
        relay._nc = None

        js = AsyncMock()

        async def _fake_ensure_connected() -> tuple[AsyncMock, object]:
            return js, object()

        async def _hung_get_nc() -> NatsClient:
            await asyncio.sleep(5.0)
            msg = "unreachable — the timeout must fire first"
            raise AssertionError(msg)

        monkeypatch.setattr(relay, "_ensure_connected", _fake_ensure_connected)
        relay.get_nc = _hung_get_nc  # type: ignore[method-assign]

        start = time.monotonic()
        await relay.deliver(Message(from_user="kai", to_user="eric", body="hi"))
        elapsed = time.monotonic() - start

        assert elapsed < 1.0  # deliver() itself never waits on the poke
        js.publish.assert_awaited_once()  # the durable publish still ran

    async def test_closed_relay_never_reconnects(self) -> None:
        """``_closed`` short-circuits before ``get_nc`` is even called."""
        relay = NatsRelay()
        relay._closed = True

        called = False

        async def _get_nc() -> NatsClient:
            nonlocal called
            called = True
            msg = "unreachable — close() must short-circuit first"
            raise AssertionError(msg)

        relay.get_nc = _get_nc  # type: ignore[method-assign]

        with pytest.raises(ConnectionError):
            await relay._live_nc_or_reconnect()

        assert called is False

    async def test_close_sets_closed_flag(self) -> None:
        relay = NatsRelay()
        assert relay._closed is False
        await relay.close()
        assert relay._closed is True

    async def test_disconnect_does_not_set_closed_flag(self) -> None:
        """``disconnect()`` is reversible — it must not trip the terminal flag."""
        relay = NatsRelay()
        await relay.disconnect()  # no live connection; must be a safe no-op
        assert relay._closed is False

    async def test_poke_after_close_does_not_create_new_connection(self) -> None:
        """A best-effort poke fired after ``close()`` must not resurrect a
        client — the end-to-end path through the public best-effort
        publish, not just the ``_live_nc_or_reconnect`` unit above.
        """
        relay = NatsRelay()
        await relay.close()

        called = False

        async def _get_nc() -> NatsClient:
            nonlocal called
            called = True
            msg = "unreachable — close() must short-circuit first"
            raise AssertionError(msg)

        relay.get_nc = _get_nc  # type: ignore[method-assign]

        await relay._publish_inbox_notification("repo", "kai")  # must not raise

        assert called is False


class TestValidateUserRejectsColon:
    """The ``inbox_notify_subject``/``talk_notify_subject`` disjointness
    docstring claims a bare user can never contain ``:`` —
    ``_validate_user`` must actually enforce that, not just assert it.
    """

    async def test_colon_bearing_user_is_rejected(self) -> None:
        relay = NatsRelay()
        with pytest.raises(ValueError, match="Invalid username"):
            relay.inbox_notify_subject(relay._repo_name, "kai:tty1")

    async def test_colon_bearing_user_cannot_collide_with_a_talk_subject(self) -> None:
        """The disjointness the docstring claims, proven directly: a
        broadcast poke subject for a forged ``user:tty`` can never equal a
        real talk-notify subject for that same session, because
        constructing the poke subject now raises before it can collide.
        """
        relay = NatsRelay()
        talk_subject = relay.talk_notify_subject("kai:tty1")
        with pytest.raises(ValueError, match="Invalid username"):
            relay.inbox_notify_subject("talk", "kai:tty1")
        # The talk subject itself is unaffected — still constructible and
        # distinct from anything the (now-rejected) poke construction
        # could have produced.
        assert talk_subject == f"{relay._stream_prefix}.talk.notify.kai:tty1"


class TestBroadcastPokeCarriesTargetRepo:
    """A cross-repo broadcast's poke subject names the TARGET repo, not
    the sender's — proven directly against ``deliver()``'s
    broadcast branch without a live server, by capturing the arguments
    ``_publish_inbox_notification`` is called with.
    """

    async def test_cross_repo_broadcast_pokes_the_target_repos_subject(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        relay = NatsRelay(repo_name="sender-repo")

        captured: list[tuple[str, str]] = []

        async def _fake_publish_inbox_notification(repo: str, user: str) -> None:
            captured.append((repo, user))

        js = AsyncMock()

        async def _fake_ensure_connected() -> tuple[AsyncMock, object]:
            return js, object()

        monkeypatch.setattr(
            relay, "_publish_inbox_notification", _fake_publish_inbox_notification
        )
        monkeypatch.setattr(relay, "_ensure_connected", _fake_ensure_connected)
        monkeypatch.setattr(relay, "_publish_talk_notification", AsyncMock())

        await relay.deliver(
            Message(from_user="kai", to_user="eric", body="cross-repo"),
            target_repo="target-repo",
        )

        assert captured == [("target-repo", "eric")]


class TestCloseRaceWithReconnect:
    """``close()`` racing a blocked reconnect must never leave a client
    installed that nothing will ever close again.

    Before this fix, ``close()`` did not take ``_connect_lock`` at all —
    a reconnect that was mid-dial when ``close()`` ran could still finish
    afterward and overwrite the ``None`` ``close()`` had just set,
    resurrecting a connection outside the terminal-close contract.
    ``close()`` now serializes on the same lock ``_ensure_connected``
    holds: whichever acquires it first runs to completion before the
    other proceeds, so the in-flight reconnect either finishes and is
    torn down cleanly by ``close()`` right after, or never starts at all
    because ``close()`` won the race and ``_ensure_connected`` sees
    ``_closed`` set inside the lock.
    """

    async def test_close_during_blocked_reconnect_installs_no_client(self) -> None:
        relay = NatsRelay()

        dial_started = asyncio.Event()
        release_dial = asyncio.Event()

        fake_nc = MagicMock()
        fake_nc.is_closed = False
        fake_nc.close = AsyncMock()

        async def _slow_dial() -> NatsClient:
            dial_started.set()
            await release_dial.wait()
            return cast("NatsClient", fake_nc)

        async def _fake_provision(
            _nc: NatsClient,
        ) -> tuple[JetStreamContext, KeyValue, KeyValue]:
            return (
                cast("JetStreamContext", MagicMock()),
                cast("KeyValue", MagicMock()),
                cast("KeyValue", MagicMock()),
            )

        relay._dial = _slow_dial  # type: ignore[method-assign]
        relay._provision = _fake_provision  # type: ignore[method-assign,assignment]

        # A reconnect starts and blocks mid-dial, holding _connect_lock.
        connect_task = asyncio.create_task(relay._ensure_connected())
        await dial_started.wait()

        # close() races in while the dial is still blocked — it must wait
        # on the same lock rather than tearing down state concurrently.
        close_task = asyncio.create_task(relay.close())
        await asyncio.sleep(0.05)  # let close() start blocking on the lock
        assert not close_task.done()

        # Let the blocked dial complete: it started first, so it legitimately
        # finishes and installs the client before close() gets the lock.
        release_dial.set()
        await connect_task  # no exception — this reconnect began before close()

        await close_task

        assert relay._closed is True
        assert relay._nc is None  # close() tore down what the reconnect installed

        with pytest.raises(ConnectionError, match="closed"):
            await relay.get_nc()

    async def test_reconnect_started_after_close_never_dials(self) -> None:
        relay = NatsRelay()
        await relay.close()

        dialed = False

        async def _dial_should_not_run() -> NatsClient:
            nonlocal dialed
            dialed = True
            msg = "unreachable — close() must be checked before dialing"
            raise AssertionError(msg)

        relay._dial = _dial_should_not_run  # type: ignore[method-assign]

        with pytest.raises(ConnectionError, match="closed"):
            await relay._ensure_connected()

        assert dialed is False


class TestOpenConnectionCancellationSafety:
    """A cancellation landing while ``_provision`` is in flight must not
    skip ``_open_connection``'s cleanup.

    Before this fix, the cleanup ``except`` clause caught only
    ``Exception`` — but ``asyncio.CancelledError`` is a ``BaseException``
    (Python 3.8+), so an outer cancellation (e.g. ``_live_nc_or_reconnect``'s
    bounding ``asyncio.timeout()``) landing mid-``_provision`` skipped the
    clause entirely: the dialed client was never closed, never installed on
    ``self._nc``, and so was leaked — neither owned nor reachable.
    """

    async def test_cancellation_during_provision_closes_the_dialed_client(
        self,
    ) -> None:
        relay = NatsRelay()

        fake_nc = MagicMock()
        fake_nc.is_closed = False
        fake_nc.close = AsyncMock()

        async def _fast_dial() -> NatsClient:
            return cast("NatsClient", fake_nc)

        provision_started = asyncio.Event()

        async def _hanging_provision(
            _nc: NatsClient,
        ) -> tuple[JetStreamContext, KeyValue, KeyValue]:
            provision_started.set()
            await asyncio.sleep(5.0)  # never resolves before the outer cancel
            msg = "unreachable — outer cancellation must fire first"
            raise AssertionError(msg)

        relay._dial = _fast_dial  # type: ignore[method-assign]
        relay._provision = _hanging_provision  # type: ignore[method-assign,assignment]

        connect_task = asyncio.create_task(relay._ensure_connected())
        await provision_started.wait()
        connect_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await connect_task

        # The dialed client must be closed, not leaked un-closed and
        # un-cached — this is the discriminator: the old code never
        # reached safe_close() for a CancelledError.
        fake_nc.close.assert_awaited_once()
        assert relay._nc is None
        assert relay._js is None
        assert relay._kv is None
        assert relay._names_kv is None
        # The lock must have been released too — a subsequent connect works.
        assert not relay._connect_lock.locked()


class TestDisconnectRaceWithReconnect:
    """``disconnect()`` racing a blocked reconnect must not let the
    reconnect silently resurrect the connection ``disconnect()`` just
    reported as torn down.

    Before this fix, ``disconnect()`` did not take ``_connect_lock`` at
    all — it could observe ``self._nc`` as ``None``, decide there was
    nothing to tear down, and return, while a reconnect that was already
    blocked mid-dial (waiting on the lock for an unrelated reason) then
    finished and installed a fresh client moments later, leaving the
    relay connected again right after ``disconnect()`` returned.
    ``disconnect()`` now serialises on the same lock ``_ensure_connected``
    holds, so whichever of the two runs first completes before the other
    proceeds.
    """

    async def test_disconnect_during_blocked_reconnect_tears_down_installed_client(
        self,
    ) -> None:
        relay = NatsRelay()

        dial_started = asyncio.Event()
        release_dial = asyncio.Event()

        fake_nc = MagicMock()
        fake_nc.is_closed = False
        fake_nc.close = AsyncMock()

        async def _slow_dial() -> NatsClient:
            dial_started.set()
            await release_dial.wait()
            return cast("NatsClient", fake_nc)

        async def _fake_provision(
            _nc: NatsClient,
        ) -> tuple[JetStreamContext, KeyValue, KeyValue]:
            return (
                cast("JetStreamContext", MagicMock()),
                cast("KeyValue", MagicMock()),
                cast("KeyValue", MagicMock()),
            )

        relay._dial = _slow_dial  # type: ignore[method-assign]
        relay._provision = _fake_provision  # type: ignore[method-assign,assignment]

        # A reconnect starts and blocks mid-dial, holding _connect_lock.
        connect_task = asyncio.create_task(relay._ensure_connected())
        await dial_started.wait()

        # disconnect() races in while the dial is still blocked — it must
        # wait on the same lock rather than tearing down (or no-oping past)
        # state concurrently.
        disconnect_task = asyncio.create_task(relay.disconnect())
        await asyncio.sleep(0.05)  # let disconnect() start blocking on the lock
        assert not disconnect_task.done()

        # Let the blocked dial complete: it started first, so it
        # legitimately finishes and installs the client before disconnect()
        # gets the lock.
        release_dial.set()
        await connect_task  # no exception — this reconnect began before disconnect()

        await disconnect_task

        # disconnect() ran AFTER the reconnect installed its client — it
        # must tear down exactly what that reconnect just installed, not
        # leave it standing because an earlier, stale read saw nothing.
        assert relay._nc is None
        fake_nc.close.assert_awaited_once()

    async def test_disconnect_before_reconnect_starts_is_still_a_safe_no_op(
        self,
    ) -> None:
        """The lock must not turn a completely ordinary no-op disconnect
        into a hang or an error — only the racing case above changes."""
        relay = NatsRelay()
        await relay.disconnect()
        assert relay._nc is None
        assert relay._closed is False
