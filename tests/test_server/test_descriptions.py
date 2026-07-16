"""Tests for dynamic tool description updates and inbox polling."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.models import BiffConfig, Message, UnreadSummary, WallPost
from biff.nats_relay import NatsRelay
from biff.relay import LocalRelay
from biff.server.app import create_server
from biff.server.state import ServerState, create_state
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import (
    _READ_MESSAGES_BASE,
    MAX_UNREAD_COUNT,
    TalkSubscription,
    _reconcile_talk_sub,
    _talk_description,
    _write_unread_file,
    poll_inbox,
    refresh_read_messages,
    subscribe_talk,
    talk_signal,
)
from biff.server.tools.wall import WALL_BASE_DESCRIPTION
from biff.talk_resubscribe import TalkResubscribeLatch
from biff.talk_state import TalkState

if TYPE_CHECKING:
    from fastmcp import FastMCP

_TEST_REPO = "_test-server"
_KAI_SESSION = "kai:tty1"
_TALK_LOGGER = "test.talk_resubscribe"


def _test_latch() -> TalkResubscribeLatch:
    """A fresh latch for reconcile tests that do not assert on its logs."""
    return TalkResubscribeLatch(logging.getLogger(_TALK_LOGGER))


@pytest.fixture
def state(tmp_path: Path) -> ServerState:
    return create_state(
        BiffConfig(user="kai", repo_name=_TEST_REPO),
        tmp_path,
        tty="tty1",
        hostname="test-host",
        pwd="/test",
    )


class TestRefreshReadMessages:
    async def test_no_messages_uses_base(self, state: ServerState) -> None:
        mcp = create_server(state)
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE

    async def test_unread_shows_count(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="auth module ready",
            )
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        assert "Marks all as read." in desc

    async def test_multiple_unread(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="first")
        )
        await state.relay.deliver(
            Message(from_user="priya", to_user=_KAI_SESSION, body="second")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "2 unread" in desc

    async def test_reverts_to_base_when_cleared(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        desc = tool.description
        assert desc is not None
        assert "1 unread" in desc
        # Mark as read
        unread = await state.relay.fetch(_KAI_SESSION)
        await state.relay.mark_read(_KAI_SESSION, [m.id for m in unread])
        await refresh_read_messages(mcp, state)
        assert tool.description == _READ_MESSAGES_BASE

    async def test_ignores_other_users_messages(self, state: ServerState) -> None:
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="kai", to_user="eric:tty2", body="for eric")
        )
        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE


class TestUnreadFile:
    """Verify unread.json is written for status bar consumption."""

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=tmp_path / "unread.json",
        )

    async def test_writes_unread_file(self, state_with_path: ServerState) -> None:
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="auth ready",
            )
        )
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        assert "preview" not in data

    async def test_writes_zero_when_no_messages(
        self, state_with_path: ServerState
    ) -> None:
        mcp = create_server(state_with_path)
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_reverts_to_zero_after_read(
        self, state_with_path: ServerState
    ) -> None:
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )
        await refresh_read_messages(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1
        # Mark as read
        unread = await state_with_path.relay.fetch(_KAI_SESSION)
        await state_with_path.relay.mark_read(_KAI_SESSION, [m.id for m in unread])
        await refresh_read_messages(mcp, state_with_path)
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_clamps_unread_count_at_max(self, tmp_path: Path) -> None:
        path = tmp_path / "unread.json"
        summary = UnreadSummary(count=999)
        _write_unread_file(
            path,
            summary,
            repo_name=_TEST_REPO,
            user="kai",
            tty_name="tty1",
            biff_enabled=True,
        )
        data = json.loads(path.read_text())
        assert data["count"] == MAX_UNREAD_COUNT

    async def test_no_write_when_path_is_none(self, state: ServerState) -> None:
        assert state.unread_path is None
        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="test")
        )
        await refresh_read_messages(mcp, state)
        # No error — function completes without attempting file write

    async def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "nested" / "unread.json"
        config = BiffConfig(user="kai", repo_name=_TEST_REPO)
        state = create_state(
            config,
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=nested,
        )
        mcp = create_server(state)
        await refresh_read_messages(mcp, state)
        assert nested.exists()


class TestTalkSignal:
    """``talk_signal`` keys on invite identity so same-count churn is detected."""

    def _talk(self, tmp_path: Path) -> TalkState:
        return TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )

    @staticmethod
    def _invite(from_key: str) -> dict[str, str]:
        return {
            "type": "invite",
            "from": "eric",
            "from_key": from_key,
            "to_key": "kai:t",
        }

    def test_same_count_session_churn_changes_signal(self, tmp_path: Path) -> None:
        """One inviter session superseded by another keeps the count but changes
        the signal, so the poller refreshes and never leaves a stale accept hint.
        """
        talk = self._talk(tmp_path)
        talk.receive(self._invite("eric:aaa"))
        talk.drain_idle()
        before = talk_signal(talk)
        talk.receive(self._invite("eric:bbb"))  # supersedes; count stays 1
        talk.drain_idle()
        after = talk_signal(talk)
        assert len(talk.pending_invites) == 1
        assert before != after


class TestTalkDescriptionAcceptHint:
    """The ``[TALK]`` marker names the inviter's session by its display tty.

    The accept hint must read as the ``@user:ttyN`` address ``/who`` shows —
    the form ``talk @user:ttyN`` resolves against — not the opaque session-key
    hex the inviter's session actually keys on.  Same source as
    ``format_agent_drain`` (``PendingInvite.accept_command``), so both surfaces
    stay reconciled.
    """

    def test_marker_renders_display_tty_not_key_hex(self, tmp_path: Path) -> None:
        talk = TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )
        talk.receive(
            {
                "type": "invite",
                "from": "jfreeman",
                "from_tty": "tty6",
                "from_key": "jfreeman:75abc665",
                "to_key": "kai:t",
            }
        )
        talk.drain_idle()  # records the pending invite

        desc = _talk_description(talk)

        assert "talk @jfreeman:tty6" in desc
        assert "75abc665" not in desc


class TestTalkDescriptionQueuedInvite:
    """A queued (undrained) invite reads as an invite, not a chat message.

    An unsolicited invite lands in the queue before ``talk_read`` moves it into
    ``pendingInvites``.  Rendering that queued frame as "N new message" would
    tell the agent to read a message when it should accept a talk; the marker
    must read "wants to talk" for a queued invite and "new message" only for a
    queued chat message.  Both light ``[TALK]``.
    """

    def _talk(self, tmp_path: Path) -> TalkState:
        return TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )

    def test_queued_invite_reads_as_invite(self, tmp_path: Path) -> None:
        talk = self._talk(tmp_path)
        talk.receive(
            {
                "type": "invite",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def67890",
                "to_key": "kai:t",
            }
        )  # left undrained — still in the queue, not yet in pendingInvites

        desc = _talk_description(talk)

        assert "[TALK]" in desc
        assert "eric wants to talk" in desc
        assert "new message" not in desc

    def test_queued_chat_message_reads_as_message(self, tmp_path: Path) -> None:
        talk = self._talk(tmp_path)
        talk.receive(
            {
                "type": "message",
                "from": "eric",
                "from_tty": "tty2",
                "from_key": "eric:def67890",
                "body": "hi",
                "to_key": "kai:t",
            }
        )

        desc = _talk_description(talk)

        assert "[TALK]" in desc
        assert "1 new message" in desc
        assert "wants to talk" not in desc


class TestTalkDescriptionConnectedHint:
    """The connected ``[TALK]`` hint names the partner's session (DES-043).

    A bare ``talk @user`` reply hint can fail resolution when the partner
    runs several sessions; the connected hint must carry the partner's tty so
    it reads as the session-scoped ``talk @user:tty`` the accept path emits.
    """

    def test_connected_hint_carries_partner_tty(self, tmp_path: Path) -> None:
        talk = TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )
        talk.begin_connected(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )

        desc = _talk_description(talk)

        assert "talk @jfreeman:tty6" in desc
        assert "talk @jfreeman <" not in desc

    def test_connected_hint_falls_back_to_bare_without_tty(
        self, tmp_path: Path
    ) -> None:
        talk = TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )
        talk.begin_connected(
            partner="jfreeman", partner_tty="", partner_key="jfreeman:75abc665"
        )

        desc = _talk_description(talk)

        assert "talk @jfreeman <message>" in desc


class TestPollInbox:
    """Verify the background inbox poller detects changes and refreshes."""

    _FAST_INTERVAL = 0.01

    @pytest.fixture
    def state_with_path(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=tmp_path / "unread.json",
        )

    async def _run_poller(
        self,
        mcp: FastMCP[ServerState],
        state: ServerState,
        *,
        cycles: int = 5,
    ) -> None:
        """Run the poller for a few cycles then cancel it."""
        task = asyncio.create_task(poll_inbox(mcp, state, interval=self._FAST_INTERVAL))
        await asyncio.sleep(self._FAST_INTERVAL * cycles)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def test_initial_refresh_writes_file(
        self, state_with_path: ServerState
    ) -> None:
        """Poller forces a refresh on its first cycle (last_count=-1)."""
        mcp = create_server(state_with_path)
        await self._run_poller(mcp, state_with_path)
        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 0

    async def test_detects_new_message(self, state_with_path: ServerState) -> None:
        """Poller picks up a message added between poll cycles."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial cycle run
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        # Inject a message
        await state_with_path.relay.deliver(
            Message(
                from_user="eric",
                to_user=_KAI_SESSION,
                body="PR ready",
            )
        )
        # Let poller detect the change
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert state_with_path.unread_path is not None
        data = json.loads(state_with_path.unread_path.read_text())
        assert data["count"] == 1

    async def test_updates_tool_description(self, state_with_path: ServerState) -> None:
        """Poller updates the read_messages tool description."""
        mcp = create_server(state_with_path)
        await state_with_path.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="lunch?")
        )
        await self._run_poller(mcp, state_with_path)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert "1 unread" in (tool.description or "")

    async def test_skips_refresh_when_unchanged(
        self, state_with_path: ServerState
    ) -> None:
        """Poller does not rewrite the file when count is stable."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial refresh write the file
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        assert state_with_path.unread_path is not None
        mtime_after_initial = state_with_path.unread_path.stat().st_mtime_ns
        # Let several more cycles run — count stays at 0
        await asyncio.sleep(self._FAST_INTERVAL * 10)
        mtime_after_stable = state_with_path.unread_path.stat().st_mtime_ns
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert mtime_after_stable == mtime_after_initial

    async def test_talk_subscription_retries_after_initial_failure(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed first subscribe_talk is retried until it succeeds.

        A NATS outage during server startup makes the first subscribe_talk
        return None; without a retry the whole talk channel stays silently
        disabled for the server's lifetime even after NATS recovers.
        """
        fake_sub = AsyncMock()
        calls = 0

        async def flaky_subscribe(
            state: ServerState, latch: TalkResubscribeLatch
        ) -> TalkSubscription | None:
            nonlocal calls
            calls += 1
            # LocalRelay has no generation, so _relay_generation is 0 — bind the
            # SUB at 0 so the reconcile leaves it alone once it succeeds.
            return None if calls == 1 else TalkSubscription(fake_sub, 0)

        monkeypatch.setattr(_descriptions, "subscribe_talk", flaky_subscribe)
        mcp = create_server(state_with_path)
        await self._run_poller(mcp, state_with_path, cycles=8)
        assert calls >= 2  # retried past the initial None
        # Establishing the subscription is proven by its clean teardown on exit.
        fake_sub.unsubscribe.assert_awaited_once()

    async def test_generation_bump_during_tick_reconciles_same_tick(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A generation bump inside ``_safe_tick`` is rebound the SAME tick.

        The tick's relay calls are what trigger the wedge teardown that advances
        ``connection_generation``.  Reconciling AFTER the tick rebinds the SUB in
        the same iteration; reconciling before defers it a full poll interval —
        minutes of dead talk on the agent-facing MCP path.

        The tick sets the shutdown event, so the loop exits at the top of the
        NEXT iteration.  With the reconcile after the tick the re-subscribe to
        the bumped generation still runs before that exit; with the reconcile
        before the tick it never runs — the discriminating observation.
        """
        gen = [0]
        events: list[tuple[str, int]] = []
        shutdown = asyncio.Event()

        def _gen(_state: ServerState) -> int:
            return gen[0]

        async def fake_subscribe(
            _state: ServerState, _latch: TalkResubscribeLatch
        ) -> TalkSubscription:
            events.append(("subscribe", gen[0]))
            return TalkSubscription(AsyncMock(), gen[0])

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            events.append(("tick", gen[0]))
            if gen[0] == 0:
                gen[0] = 1  # the tick's relay calls trigger _force_reconnect
                shutdown.set()  # stop after this iteration completes
            return last_count, last_wall, last_talk

        monkeypatch.setattr(_descriptions, "subscribe_talk", fake_subscribe)
        monkeypatch.setattr(_descriptions, "_relay_generation", _gen)
        monkeypatch.setattr(_descriptions, "_safe_tick", fake_tick)

        mcp = create_server(state_with_path)
        await poll_inbox(
            mcp, state_with_path, shutdown=shutdown, interval=self._FAST_INTERVAL
        )

        assert ("subscribe", 1) in events  # rebound to the bumped generation
        # …and rebound before any further tick — same iteration as the bump.
        assert events == [("subscribe", 0), ("tick", 0), ("subscribe", 1)]

    async def test_cancellation_is_clean(self, state_with_path: ServerState) -> None:
        """Cancelling the poller task does not raise."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        await asyncio.sleep(self._FAST_INTERVAL * 2)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        assert task.done()

    async def test_poller_detects_wall_post(self, state_with_path: ServerState) -> None:
        """Poller detects a wall posted between cycles and updates tool description."""
        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let initial cycle run
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        tool = await mcp.get_tool("wall")
        assert tool is not None
        assert tool.description == WALL_BASE_DESCRIPTION

        # Post a wall directly via relay
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        wall = WallPost(
            text="deploy freeze",
            from_user="eric",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await state_with_path.relay.set_wall(wall)
        # Let poller detect the change
        await asyncio.sleep(self._FAST_INTERVAL * 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert tool.description is not None
        assert "deploy freeze" in tool.description
        assert "[WALL]" in tool.description

    async def test_poller_detects_wall_clear(
        self, state_with_path: ServerState
    ) -> None:
        """Poller detects a cleared wall and reverts tool description."""
        mcp = create_server(state_with_path)
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        wall = WallPost(
            text="freeze",
            from_user="eric",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await state_with_path.relay.set_wall(wall)

        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        # Let poller pick up the wall
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        tool = await mcp.get_tool("wall")
        assert tool is not None
        assert "[WALL]" in (tool.description or "")

        # Clear the wall
        await state_with_path.relay.set_wall(None)
        await asyncio.sleep(self._FAST_INTERVAL * 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert tool.description == WALL_BASE_DESCRIPTION


def _fixed_generation(value: int) -> Callable[[ServerState], int]:
    """Return a ``_relay_generation`` stand-in that always reports *value*."""

    def _gen(_state: ServerState) -> int:
        return value

    return _gen


class TestReconcileTalkSub:
    """The generation-tracked re-subscribe: the biff-3hp x biff-9la fix.

    ``nats-relay.tex`` ``talkSubGen``: the always-on talk SUB must be
    re-established when the relay dials a new client (``_force_reconnect`` /
    ``_on_closed`` orphan it on the closed client) but left untouched on an
    in-place nats-py reconnect (same client replays every SUB).  The
    discriminator is the connection generation, never a ``sub is None`` probe.
    """

    @pytest.fixture
    def state(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
        )

    async def test_no_resubscribe_when_generation_unchanged(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-place reconnect keeps the generation, so the SUB is left as-is.

        nats-py replays every SUB on the same client — re-subscribing would
        leak a duplicate.  This is the case the is-None probe gets right by
        accident and the generation check gets right by construction.
        """
        handle = AsyncMock()
        current = TalkSubscription(handle, generation=3)
        calls = 0

        async def _sub(
            _state: ServerState, _latch: TalkResubscribeLatch
        ) -> TalkSubscription | None:
            nonlocal calls
            calls += 1
            return TalkSubscription(AsyncMock(), 3)

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(3))

        result = await _reconcile_talk_sub(state, current, _test_latch())

        assert result is current
        assert calls == 0
        handle.unsubscribe.assert_not_awaited()

    async def test_resubscribes_when_client_replaced(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A new client (generation advanced) re-establishes the SUB.

        This is the biff-9la failure the is-None probe misses: the orphaned
        handle is still non-None, so only the generation comparison fires.
        """
        stale = AsyncMock()
        fresh = TalkSubscription(AsyncMock(), 4)

        async def _sub(
            _state: ServerState, _latch: TalkResubscribeLatch
        ) -> TalkSubscription | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(4))

        result = await _reconcile_talk_sub(
            state, TalkSubscription(stale, 3), _test_latch()
        )

        assert result is fresh
        stale.unsubscribe.assert_awaited_once()

    async def test_subscribes_when_never_established(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None handle (failed initial subscribe) is retried."""
        fresh = TalkSubscription(AsyncMock(), 1)

        async def _sub(
            _state: ServerState, _latch: TalkResubscribeLatch
        ) -> TalkSubscription | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(1))

        result = await _reconcile_talk_sub(state, None, _test_latch())

        assert result is fresh

    async def test_failed_resubscribe_drops_stale(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A re-subscribe that fails still drops the orphaned SUB.

        Returning None (not the stale handle) makes the next tick retry via the
        never-established path rather than hold a handle on the dead client.
        """
        stale = AsyncMock()

        async def _sub(
            _state: ServerState, _latch: TalkResubscribeLatch
        ) -> TalkSubscription | None:
            return None

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(5))

        result = await _reconcile_talk_sub(
            state, TalkSubscription(stale, 2), _test_latch()
        )

        assert result is None
        stale.unsubscribe.assert_awaited_once()


class TestSubscribeTalkLatch:
    """The poller's ``subscribe_talk`` routes failures/successes through the latch.

    A NATS outage fails the re-subscribe on every tick; without the latch the
    old code logged a WARNING+traceback per tick (a flood).  The latch surfaces
    the onset once at WARNING, keeps retries at DEBUG, and logs one INFO on
    recovery — the same onset/recovery discipline as ``_ConnectionHealth``.
    """

    @staticmethod
    def _nats_state(tmp_path: Path) -> tuple[ServerState, MagicMock]:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        relay.connection_generation = 1
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.kai")
        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )
        return state, relay

    async def test_failure_then_recovery_logs_once_each(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state, relay = self._nats_state(tmp_path)
        latch = TalkResubscribeLatch(logging.getLogger(_TALK_LOGGER))

        with caplog.at_level(logging.DEBUG, logger=_TALK_LOGGER):
            assert await subscribe_talk(state, latch) is None  # onset — WARNING
            assert await subscribe_talk(state, latch) is None  # retry — DEBUG

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1  # not one per tick

            relay.get_nc = AsyncMock(return_value=AsyncMock())  # NATS recovers
            sub = await subscribe_talk(state, latch)

        assert sub is not None
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1  # one recovery line
