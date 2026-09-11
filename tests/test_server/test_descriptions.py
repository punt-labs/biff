"""Tests for dynamic tool description updates and inbox polling."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, cast
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
    InboxNotifySubscription,
    TalkSubscription,
    _InboxPokeGate,
    _reconcile_inbox_notify_sub,
    _reconcile_talk_sub,
    _talk_description,
    _write_unread_file,
    nap_interval_for,
    poll_inbox,
    refresh_read_messages,
    subscribe_inbox_notify,
    subscribe_talk,
    talk_signal,
)
from biff.server.tools.wall import WALL_BASE_DESCRIPTION
from biff.talk_latch import TalkNotifyLatch
from biff.talk_state import TalkState

if TYPE_CHECKING:
    from fastmcp import FastMCP
    from fastmcp.server.context import Context

_TEST_REPO = "_test-server"
_KAI_SESSION = "kai:tty1"
_TALK_LOGGER = "test.talk_latch"


def _test_latch() -> TalkNotifyLatch:
    """A fresh latch for reconcile tests that do not assert on its logs."""
    return TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER))


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

    async def test_relay_failure_does_not_crash_caller(
        self, state: ServerState
    ) -> None:
        """A relay hiccup here must never crash the primary tool call that
        called this as a best-effort side-channel update. Review finding:
        surfaced by a test where read_messages() had already successfully
        fetched and rendered real messages, and this side-channel call
        raising on its own separate relay call would have discarded that
        already-good result with an unhandled exception.
        """
        mcp = create_server(state)

        async def _always_fails(*_args: object, **_kwargs: object) -> UnreadSummary:
            msg = "nats: timeout"
            raise TimeoutError(msg)

        state.relay.get_unread_summary = _always_fails  # type: ignore[method-assign]
        await refresh_read_messages(mcp, state)  # must not raise
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE  # unchanged, not crashed

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

    async def test_get_session_failure_does_not_crash_caller(
        self, state_with_path: ServerState
    ) -> None:
        """Bugbot review finding: refresh_read_messages' own guard around
        get_unread_summary did not cover this function's own get_session
        call for the plan field — a relay hiccup there could still crash
        the caller's already-succeeded primary result. get_unread_summary
        succeeds here; only get_session fails, isolating this specific
        call site from the one already covered by another test.
        """
        mcp = create_server(state_with_path)

        async def _always_fails(*_args: object, **_kwargs: object) -> None:
            msg = "nats: timeout"
            raise TimeoutError(msg)

        state_with_path.relay.get_session = _always_fails  # type: ignore[method-assign]
        await refresh_read_messages(mcp, state_with_path)  # must not raise
        assert state_with_path.unread_path is not None
        assert not state_with_path.unread_path.exists()  # write skipped, not crashed

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

    The accept hint must read as the ``user:ttyN`` address ``/who`` shows —
    the form ``talk user:ttyN`` resolves against — not the opaque session-key
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

        assert "talk jfreeman:tty6" in desc
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

    A bare ``talk user`` reply hint can fail resolution when the partner
    runs several sessions; the connected hint must carry the partner's tty so
    it reads as the session-scoped ``talk user:tty`` the accept path emits.
    """

    def test_connected_hint_carries_partner_tty(self, tmp_path: Path) -> None:
        talk = TalkState(
            relay=LocalRelay(tmp_path), user="kai", tty="t", session_key="kai:t"
        )
        talk.begin_connected(
            partner="jfreeman", partner_tty="tty6", partner_key="jfreeman:75abc665"
        )

        desc = _talk_description(talk)

        assert "talk jfreeman:tty6" in desc
        assert "talk jfreeman <" not in desc

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

        assert "talk jfreeman <message>" in desc


class TestRefreshWallSenderBounds:
    """A forged sender must not blow up the ``wall`` tool description.

    Neither ``WallPost.from_user`` nor ``WallPost.from_tty`` has a
    ``max_length`` on the wire. ``refresh_wall`` composed them into the
    description with no cap at all — the third independent call site for
    this defect class, alongside ``format_wall`` and
    ``format_wall_status_line`` in ``biff.formatting``.
    """

    async def test_giant_sender_and_tty_render_bounded(
        self, state: ServerState
    ) -> None:
        from datetime import UTC, datetime, timedelta

        mcp = create_server(state)
        now = datetime.now(UTC)
        wall = WallPost(
            text="deploy freeze",
            from_user="u" * 10_000,
            from_tty="t" * 10_000,
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await _descriptions.refresh_wall(mcp, state, wall=wall)
        tool = await mcp.get_tool("wall")
        assert tool is not None
        assert tool.description is not None
        # Well under the pre-fix ~20,000+ char blowup — bounded by the
        # capped sender plus the 512-char wall text ceiling, not the
        # 10,000-char forged fields.
        assert len(tool.description) < 700
        assert "u" * 10_000 not in tool.description
        assert "t" * 10_000 not in tool.description

    async def test_giant_sender_bounded_in_vox_announcement(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The vox announcement must clip ``from_user`` too.

        A giant ``from_user`` bypassing ``sanitized_sender`` here would
        embed unboundedly in subprocess argv passed to ``vox unmute``,
        turning a forged wall into an expensive real synthesis request
        on every teammate's session with vox enabled.
        """
        from datetime import UTC, datetime, timedelta

        spoken_calls: list[str] = []

        def _capture(text: str, *, vibe_tags: str = "") -> None:
            del vibe_tags
            spoken_calls.append(text)

        monkeypatch.setattr("biff.integration.vox.speak_fire_and_forget", _capture)

        mcp = create_server(state)
        now = datetime.now(UTC)
        wall = WallPost(
            text="deploy freeze",
            from_user="u" * 10_000,
            from_tty="tty1",
            posted_at=now,
            expires_at=now + timedelta(hours=1),
        )
        await _descriptions.refresh_wall(mcp, state, wall=wall)

        assert len(spoken_calls) == 1
        assert len(spoken_calls[0]) < 700
        assert "u" * 10_000 not in spoken_calls[0]


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
            state: ServerState, latch: TalkNotifyLatch
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
            _state: ServerState, _latch: TalkNotifyLatch
        ) -> TalkSubscription:
            events.append(("subscribe", gen[0]))
            return TalkSubscription(AsyncMock(), gen[0])

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
            *,
            gate: _InboxPokeGate,
            nap_interval: float,
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            del gate, nap_interval
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

    async def test_cheap_nap_tick_reconciles_background_generation_bump(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cheap nap tick still reconciles a background-triggered client swap.

        The heartbeat loop can wedge-teardown and redial the NATS client
        *during* a nap — advancing ``connection_generation`` with no ``_safe_tick``
        involved.  If the reconcile were gated behind the nap-skip, the always-on
        talk SUB would stay orphaned on the dead client until the nap ended, and
        an unsolicited invite to the idle agent would be silently dropped.
        The reconcile must run on the cheap nap tick even though the
        expensive relay poll is skipped.
        """
        events: list[tuple[str, int]] = []
        tick_calls = [0]
        subscribe_calls = [0]

        def _gen(_state: ServerState) -> int:
            return 1  # a background swap advanced the generation past the SUB

        async def fake_subscribe(
            _state: ServerState, _latch: TalkNotifyLatch
        ) -> TalkSubscription:
            subscribe_calls[0] += 1
            # First call is the startup bind at the pre-swap generation (0, now
            # stale); the cheap-nap reconcile rebinds to the live generation (1).
            bound = 0 if subscribe_calls[0] == 1 else 1
            events.append(("subscribe", bound))
            return TalkSubscription(AsyncMock(), bound)

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
            *,
            gate: _InboxPokeGate,
            nap_interval: float,
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            del gate, nap_interval
            tick_calls[0] += 1
            return last_count, last_wall, last_talk

        monkeypatch.setattr(_descriptions, "subscribe_talk", fake_subscribe)
        monkeypatch.setattr(_descriptions, "_relay_generation", _gen)
        monkeypatch.setattr(_descriptions, "_safe_tick", fake_tick)

        # Force a cheap nap tick: napping, with a recent nap poll so the
        # seconds-since-nap-poll guard holds under a large nap_interval.
        state_with_path.activity.enter_nap()
        state_with_path.activity.record_nap_poll()

        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(
                mcp,
                state_with_path,
                interval=self._FAST_INTERVAL,
                nap_interval=1000.0,
            )
        )
        await asyncio.sleep(self._FAST_INTERVAL * 5)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert ("subscribe", 1) in events  # rebound on the cheap nap tick
        assert tick_calls[0] == 0  # _safe_tick skipped — the cheap nap held

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

    async def test_establishes_and_tears_down_both_subs(self, tmp_path: Path) -> None:
        """poll_inbox opens talk AND inbox-notify SUBs, unsubscribes both on exit.

        DES-062: the second always-on SUB must be established alongside the
        existing talk SUB and torn down the same way — proven here against a
        mocked ``NatsRelay`` (LocalRelay, used by every other test in this
        class, has no push mechanism at all — see
        ``_relay_pushes_inbox_notify``).
        """
        nc = AsyncMock()
        talk_handle = AsyncMock()
        inbox_handle = AsyncMock()

        async def fake_subscribe(subject: str, *, cb: object) -> AsyncMock:
            del cb
            return talk_handle if "talk" in subject else inbox_handle

        nc.subscribe = AsyncMock(side_effect=fake_subscribe)

        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.inbox_notify_subject = MagicMock(
            return_value="biff-dev._test-server.inbox.notify.kai"
        )
        relay.get_wall = AsyncMock(return_value=None)
        relay.get_unread_summary = AsyncMock(return_value=UnreadSummary(count=0))

        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state, interval=self._FAST_INTERVAL))
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        subjects = [c.args[0] for c in nc.subscribe.call_args_list]
        assert any("talk" in s for s in subjects)
        assert any("inbox.notify" in s for s in subjects)
        talk_handle.unsubscribe.assert_awaited_once()
        inbox_handle.unsubscribe.assert_awaited_once()


def _fixed_generation(value: int) -> Callable[[ServerState], int]:
    """Return a ``_relay_generation`` stand-in that always reports *value*."""

    def _gen(_state: ServerState) -> int:
        return value

    return _gen


class TestReconcileTalkSub:
    """The generation-tracked re-subscribe: the wedge x orphaned-SUB fix.

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
            _state: ServerState, _latch: TalkNotifyLatch
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

        This is the failure the is-None probe misses: the orphaned
        handle is still non-None, so only the generation comparison fires.
        """
        stale = AsyncMock()
        fresh = TalkSubscription(AsyncMock(), 4)

        async def _sub(
            _state: ServerState, _latch: TalkNotifyLatch
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
            _state: ServerState, _latch: TalkNotifyLatch
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
            _state: ServerState, _latch: TalkNotifyLatch
        ) -> TalkSubscription | None:
            return None

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(5))

        result = await _reconcile_talk_sub(
            state, TalkSubscription(stale, 2), _test_latch()
        )

        assert result is None
        stale.unsubscribe.assert_awaited_once()


class TestNapIntervalFor:
    """The backstop cadence scales with the configured poll interval."""

    def test_default_matches_historical_nap_interval(self) -> None:
        """The module defaults (2.0s / 30.0s) round-trip exactly."""
        assert nap_interval_for(2.0) == 30.0

    def test_scales_proportionally(self) -> None:
        assert nap_interval_for(1.0) == 15.0
        assert nap_interval_for(10.0) == 150.0


class TestInboxPokeGate:
    """Poke-gated recompute with a periodic backstop (DES-062)."""

    def test_starts_poked_forces_initial_recompute(self) -> None:
        """A fresh gate recomputes on its first check — no poke needed.

        Replaces the old ``last_count = -1`` "force initial refresh" idiom.
        """
        gate = _InboxPokeGate()
        assert gate.should_recompute(backstop_interval=1000.0) is True

    def test_no_recompute_before_backstop_with_no_poke(self) -> None:
        """An unpoked tick within the backstop window does not recompute."""
        gate = _InboxPokeGate()
        gate.recompute_done()  # consume the initial forced recompute
        assert gate.should_recompute(backstop_interval=1000.0) is False

    def test_poke_forces_recompute_regardless_of_backstop(self) -> None:
        """A marked poke recomputes immediately, even mid-backstop-window."""
        gate = _InboxPokeGate()
        gate.recompute_done()
        gate.mark()
        assert gate.should_recompute(backstop_interval=1000.0) is True

    def test_backstop_recomputes_with_no_poke(self) -> None:
        """The backstop cadence fires a recompute even with no poke at all.

        This is the dropped-poke insurance: an at-most-once core-NATS poke
        that never arrives still gets picked up within one backstop
        interval instead of stalling forever.
        """
        gate = _InboxPokeGate()
        gate.recompute_done()
        assert gate.should_recompute(backstop_interval=0.0) is True

    def test_recompute_done_clears_poke_and_resets_clock(self) -> None:
        """recompute_done() clears the poke flag and restarts the backstop clock."""
        gate = _InboxPokeGate()
        gate.mark()
        gate.recompute_done()
        assert gate.should_recompute(backstop_interval=1000.0) is False


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
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER))

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


class TestSubscribeInboxNotify:
    """``subscribe_inbox_notify`` establishes the broadcast wake-poke SUB.

    Mirrors ``TestSubscribeTalkLatch``: a NATS outage routes failure/success
    through the latch (onset WARNING, retries DEBUG, one recovery INFO).
    """

    @staticmethod
    def _nats_state(tmp_path: Path) -> tuple[ServerState, MagicMock]:
        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(side_effect=TimeoutError("wedged"))
        relay.connection_generation = 1
        relay.inbox_notify_subject = MagicMock(
            return_value="biff-dev._test-server.inbox.notify.kai"
        )
        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )
        return state, relay

    async def test_returns_none_for_non_nats_relay(self, state: ServerState) -> None:
        """LocalRelay (the default test relay) has no push mechanism."""
        gate = _InboxPokeGate()
        result = await subscribe_inbox_notify(state, _test_latch(), gate)
        assert result is None

    async def test_subscribes_on_the_repo_scoped_subject(self, tmp_path: Path) -> None:
        state, relay = self._nats_state(tmp_path)
        relay.get_nc = AsyncMock(return_value=AsyncMock())
        gate = _InboxPokeGate()
        sub = await subscribe_inbox_notify(state, _test_latch(), gate)
        assert sub is not None
        assert sub.generation == 1
        relay.inbox_notify_subject.assert_called_once_with("_test-server", "kai")

    async def test_callback_marks_gate_and_wakes(self, tmp_path: Path) -> None:
        """The callback marks the poke gate and wakes the poller — nothing else."""
        state, relay = self._nats_state(tmp_path)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        gate = _InboxPokeGate()
        gate.recompute_done()  # consume the initial forced recompute
        state.activity.enter_nap()

        await subscribe_inbox_notify(state, _test_latch(), gate)
        callback = nc.subscribe.call_args.kwargs["cb"]
        await callback(object())

        assert gate.should_recompute(backstop_interval=1000.0) is True
        assert state.activity.napping is False  # wake() exited napping

    async def test_failure_then_recovery_logs_once_each(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state, relay = self._nats_state(tmp_path)
        gate = _InboxPokeGate()
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER))

        with caplog.at_level(logging.DEBUG, logger=_TALK_LOGGER):
            assert await subscribe_inbox_notify(state, latch, gate) is None
            assert await subscribe_inbox_notify(state, latch, gate) is None

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1  # not one per tick

            relay.get_nc = AsyncMock(return_value=AsyncMock())  # NATS recovers
            sub = await subscribe_inbox_notify(state, latch, gate)

        assert sub is not None
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1  # one recovery line


class TestReconcileInboxNotifySub:
    """The generation-tracked re-subscribe, generalized to the inboxNotify kind.

    Same fix as ``TestReconcileTalkSub``, applied to the second always-on
    SUB the ``SubKind``-indexed family (``nats-relay.tex`` ``subGen``) adds.
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
        handle = AsyncMock()
        current = InboxNotifySubscription(handle, generation=3)
        calls = 0

        async def _sub(
            _state: ServerState, _latch: TalkNotifyLatch, _gate: _InboxPokeGate
        ) -> InboxNotifySubscription | None:
            nonlocal calls
            calls += 1
            return InboxNotifySubscription(AsyncMock(), 3)

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(3))

        result = await _reconcile_inbox_notify_sub(
            state, current, _test_latch(), _InboxPokeGate()
        )

        assert result is current
        assert calls == 0
        handle.unsubscribe.assert_not_awaited()

    async def test_resubscribes_when_client_replaced(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = AsyncMock()
        fresh = InboxNotifySubscription(AsyncMock(), 4)

        async def _sub(
            _state: ServerState, _latch: TalkNotifyLatch, _gate: _InboxPokeGate
        ) -> InboxNotifySubscription | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(4))

        result = await _reconcile_inbox_notify_sub(
            state, InboxNotifySubscription(stale, 3), _test_latch(), _InboxPokeGate()
        )

        assert result is fresh
        stale.unsubscribe.assert_awaited_once()

    async def test_subscribes_when_never_established(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fresh = InboxNotifySubscription(AsyncMock(), 1)

        async def _sub(
            _state: ServerState, _latch: TalkNotifyLatch, _gate: _InboxPokeGate
        ) -> InboxNotifySubscription | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(1))

        result = await _reconcile_inbox_notify_sub(
            state, None, _test_latch(), _InboxPokeGate()
        )

        assert result is fresh


class TestStartupNotificationRace:
    """Pre-existing unread messages must notify the client after session capture.

    The lifespan calls ``refresh_read_messages`` before the MCP client has
    sent ``initialize`` — so ``_session`` is ``None`` and the
    ``tools/list_changed`` notification is silently dropped.  By the time
    ``_SessionCaptureMiddleware`` stores ``_session``, the tool description
    text is already correct, so subsequent refreshes see no change and never
    re-notify.  The client never learns about the unread messages.
    """

    @pytest.fixture()
    def state(self, tmp_path: Path) -> ServerState:
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
        )

    async def test_pre_existing_messages_notify_after_session_capture(
        self, state: ServerState
    ) -> None:
        """After session capture, a notification must fire for any description
        that diverged from its base during the pre-session lifespan window.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)

        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert "1 unread" in (tool.description or "")

        fake_session = MagicMock(spec=ServerSession)
        fake_session.send_tool_list_changed = AsyncMock()
        await _descriptions.capture_session(fake_session)

        fake_session.send_tool_list_changed.assert_awaited_once()

    async def test_poller_cannot_recover_lost_notification(
        self, state: ServerState
    ) -> None:
        """The poller's first tick after session capture sees no description
        change and skips re-notification — proving the poller alone cannot
        recover from the lost startup notification.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)

        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hi")
        )

        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert "1 unread" in (tool.description or "")

        fake_session = MagicMock(spec=ServerSession)
        fake_session.send_tool_list_changed = AsyncMock()
        await _descriptions.capture_session(fake_session)

        fake_session.send_tool_list_changed.reset_mock()
        await refresh_read_messages(mcp, state)
        fake_session.send_tool_list_changed.assert_not_awaited()

    async def test_no_spurious_notify_when_inbox_empty_at_startup(
        self, state: ServerState
    ) -> None:
        """When no messages exist at startup, session capture must not fire
        a spurious notification — the description never diverged from base.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)

        await refresh_read_messages(mcp, state)
        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE

        fake_session = MagicMock(spec=ServerSession)
        fake_session.send_tool_list_changed = AsyncMock()
        await _descriptions.capture_session(fake_session)

        fake_session.send_tool_list_changed.assert_not_awaited()

    async def test_flush_failure_clears_session_and_rearms_pending_notify(
        self, state: ServerState
    ) -> None:
        """When the flush in capture_session raises, the exception must not
        propagate, _session must be cleared (broken session ref), and
        _pending_notify must be RE-ARMED — the reconnected session was
        itself broken, so the drop is still unrecovered and must be
        retried on the next reconnect or belt call, not silently
        consumed. capture_session runs once per client ``initialize``, so
        re-arming costs one retry per reconnect, not an infinite loop.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)

        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="boom")
        )

        await refresh_read_messages(mcp, state)

        fake_session = MagicMock(spec=ServerSession)
        fake_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        await _descriptions.capture_session(fake_session)

        assert _descriptions._session is None
        assert _descriptions._pending_notify


class TestMidSessionDropRecovery:
    """A mid-session suspenders drop must be recorded and flushed, never lost.

    notification.tex sec:sessionloss: the suspenders send-failure path
    (``PollTickNotifyFail``) must set ``pendingNotify`` rather than merely
    clearing the dead session, and the belt path (``NotifyBelt``) must flush
    any such pending notify on its own next successful send. Regression
    coverage for the fix, alongside the empirical repro in
    ``test_notify_reliability_repro.py`` (kept passing unchanged).
    """

    async def test_drop_then_reconnect_flushes(self, state: ServerState) -> None:
        """A suspenders send failure followed by a reconnect (session
        recapture) flushes the notification the client never saw — the
        two-step recovery path when the client's own MCP transport dies and
        Claude Code re-initializes a fresh session.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # drops the notification
        dying_session.send_tool_list_changed.assert_awaited_once()
        # Invariant pinned here: a recorded drop always leaves the dead
        # session reference cleared — nothing must observe
        # _pending_notify=True alongside a stale, still-set _session.
        # Read _session through an ``object``-typed local rather than
        # comparing `_descriptions._session is None` directly: mypy
        # narrows the direct `_descriptions._session = dying_session`
        # assignment above as persisting past the intervening await (it
        # does not know ``refresh_read_messages`` mutates that module
        # attribute), so a same-function `is None` comparison is deemed
        # statically unreachable and poisons every statement after it.
        assert _descriptions._pending_notify
        session_after_drop: object = _descriptions._session
        assert session_after_drop is None

        reconnected = MagicMock(spec=ServerSession)
        reconnected.send_tool_list_changed = AsyncMock()
        await _descriptions.capture_session(reconnected)

        reconnected.send_tool_list_changed.assert_awaited_once()
        assert not _descriptions._pending_notify

    async def test_drop_then_belt_tool_call_flushes(self, state: ServerState) -> None:
        """A suspenders send failure followed by an in-request (belt-path)
        notify flushes the pending drop — the recovery path when the client's
        transport survives and the agent simply makes another tool call
        before any reconnect happens.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # drops the notification
        assert _descriptions._pending_notify

        sent: list[object] = []

        class _FakeContext:
            session = object()

            async def send_notification(self, notification: object) -> None:
                sent.append(notification)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_context", lambda: _FakeContext()
        )
        try:
            await _descriptions.notify_tool_list_changed()  # belt path fires
        finally:
            monkeypatch.undo()

        assert len(sent) == 1
        assert not _descriptions._pending_notify

    async def test_belt_flush_does_not_double_send(self, state: ServerState) -> None:
        """The belt path's flush is folded into its own single send — a
        pending drop must not trigger a second notification on top of the
        belt call's own.
        """
        sent: list[object] = []

        class _FakeContext:
            session = object()

            async def send_notification(self, notification: object) -> None:
                sent.append(notification)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_context", lambda: _FakeContext()
        )
        _descriptions._pending_notify = True
        try:
            await _descriptions.notify_tool_list_changed()
        finally:
            monkeypatch.undo()

        assert len(sent) == 1  # exactly one send, not two
        assert not _descriptions._pending_notify

    async def test_no_op_refresh_after_drop_leaves_pending_notify_set(
        self, state: ServerState
    ) -> None:
        """A refresh whose description does not change must not disturb a
        drop recorded by an earlier failure.

        refresh_read_messages only calls notify_tool_list_changed() when
        ``tool.description != old_desc`` — a repeated call with the same
        unread count is a pure no-op on that gate.  A drop recorded before
        this call must still be waiting for its next real flush
        opportunity, not silently lost because nothing new happened.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # drops the notification
        assert _descriptions._pending_notify

        dying_session.send_tool_list_changed.reset_mock()
        await refresh_read_messages(mcp, state)  # no-op: unread count unchanged

        # The change-gate skipped notify entirely — nothing was sent, and
        # nothing about the recorded drop moved.
        dying_session.send_tool_list_changed.assert_not_awaited()
        assert _descriptions._pending_notify

    async def test_drop_then_genuine_belt_refresh_flushes(
        self, state: ServerState
    ) -> None:
        """Belt recovery through the real entry point: after a drop, the
        message is genuinely marked read (not a synthetic re-trigger), and
        ``refresh_read_messages`` — the function every belt-path tool
        calls — is invoked directly inside a working request context.  The
        pending drop must flush alongside that call's own notification.
        """
        from mcp.server.session import ServerSession

        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session

        await refresh_read_messages(mcp, state)  # drops the notification
        assert _descriptions._pending_notify

        # Genuinely change the count/description — mark the message read,
        # exactly as the read_messages tool does — rather than synthesizing
        # a description change.
        unread = await state.relay.fetch(state.session_key)
        await state.relay.mark_read(state.session_key, [m.id for m in unread])

        sent: list[object] = []

        class _FakeContext:
            session = object()

            async def send_notification(self, notification: object) -> None:
                sent.append(notification)

        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setattr(
            "fastmcp.server.dependencies.get_context", lambda: _FakeContext()
        )
        try:
            await refresh_read_messages(mcp, state)  # real belt-path entry point
        finally:
            monkeypatch.undo()

        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert tool.description == _READ_MESSAGES_BASE  # count genuinely dropped to 0
        assert len(sent) == 1  # description change's own send carries the flush
        assert not _descriptions._pending_notify

    async def test_concurrent_suspenders_fail_and_belt_send_do_not_clobber_pending(
        self, state: ServerState
    ) -> None:
        """A suspenders failure racing a concurrent, still in-flight belt
        send must not corrupt ``_pending_notify``.

        Without ``_notify_lock``, a belt send that started before a
        suspenders failure but *completes* after it would unconditionally
        clear ``_pending_notify = False`` on completion, silently
        discarding the drop the suspenders branch just recorded — the
        notifyLost-forever failure mode, reached here via a race between
        two concurrent tasks instead of a single code path (confirmed
        empirically against the pre-lock code: given a real chance to run
        to completion, the suspenders
        task's failure lands first, and the belt send's later,
        unconditional success then clobbers it). The belt task runs
        under its own copy of FastMCP's ``_current_context`` contextvar
        (real request scoping, not a monkeypatch of ``get_context``) so
        the concurrently-running suspenders task — ambient context, no
        request in flight — takes the other branch exactly as two real
        concurrent calls would.
        """
        from fastmcp.server.context import _current_context
        from mcp.server.session import ServerSession

        mcp = create_server(state)
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="hello")
        )

        belt_send_started = asyncio.Event()
        release_belt_send = asyncio.Event()

        class _SlowContext:
            session = object()

            async def send_notification(self, notification: object) -> None:
                belt_send_started.set()
                await release_belt_send.wait()

        belt_context = contextvars.copy_context()
        belt_context.run(_current_context.set, cast("Context", _SlowContext()))
        belt_task = asyncio.create_task(
            _descriptions.notify_tool_list_changed(), context=belt_context
        )
        await belt_send_started.wait()  # belt now holds _notify_lock, mid-send

        dying_session = MagicMock(spec=ServerSession)
        dying_session.send_tool_list_changed = AsyncMock(
            side_effect=RuntimeError("transport closed")
        )
        _descriptions._session = dying_session
        suspenders_task = asyncio.create_task(refresh_read_messages(mcp, state))
        # Give the suspenders task every opportunity to run to completion
        # before releasing belt — a single scheduler turn is not enough
        # (LocalRelay's chain of awaits needs several). Under the lock it
        # will never finish this loop (it blocks acquiring _notify_lock,
        # which belt still holds) and the loop just spins to its bound;
        # without the lock, nothing blocks it and it completes within a
        # handful of turns — that gap is exactly what makes this a
        # meaningful race test instead of a coincidentally-ordered one.
        for _ in range(50):
            await asyncio.sleep(0)
            if suspenders_task.done():
                break

        release_belt_send.set()
        await belt_task
        await suspenders_task

        # The suspenders failure is the later-recorded, still-unresolved
        # drop — the lock forces it to apply only after the belt's success
        # is durable, so it must win: not be silently clobbered back to
        # False by the belt send's own unconditional flush-clear.
        assert _descriptions._pending_notify
        session_after_race: object = _descriptions._session
        assert session_after_race is None


class TestNotifySendTimeout:
    """A wedged send under ``_notify_lock`` must not hold the lock forever.

    notification.tex and notification-race.tex prove SAFETY treating every
    send as atomic; neither has a notion of a send that never returns.
    ``_NOTIFY_SEND_TIMEOUT`` is the liveness measure layered on top of that
    proof — bounding the lock hold turns a stalled transport into the same
    recorded drop a genuine send failure already produces (the timeout is
    caught by the same ``except Exception`` the failure path uses), rather
    than starving the poller and every belt-path tool call indefinitely.
    """

    async def test_suspenders_send_timeout_rearms_and_releases_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A suspenders send that never returns is bounded by
        ``_NOTIFY_SEND_TIMEOUT``, re-arms the drop exactly like a raised
        send failure, and — critically — releases the lock afterward, so a
        subsequent notify is not permanently wedged behind it.
        """
        from mcp.server.session import ServerSession

        # A short patched timeout, not a real wall-clock delay, keeps this
        # test fast — the hang below never resolves on its own, so only
        # the timeout ends the wait.
        monkeypatch.setattr(_descriptions, "_NOTIFY_SEND_TIMEOUT", 0.05)

        never_set = asyncio.Event()
        wedged_session = MagicMock(spec=ServerSession)

        async def _hang(*_args: object, **_kwargs: object) -> None:
            await never_set.wait()

        wedged_session.send_tool_list_changed = AsyncMock(side_effect=_hang)
        _descriptions._session = wedged_session

        # Wrapped in an outer wait_for as a safety net only: if the inner
        # timeout failed to bound the send, this call would hang forever
        # instead of failing the test with a clear timeout error.
        await asyncio.wait_for(_descriptions.notify_tool_list_changed(), timeout=5.0)

        wedged_session.send_tool_list_changed.assert_awaited_once()
        # Re-armed exactly like a raised send failure — the timeout routes
        # into the same failure->re-arm path, not a new one.
        assert _descriptions._pending_notify
        session_after_timeout: object = _descriptions._session
        assert session_after_timeout is None  # dead/wedged session cleared

        # Prove the lock was released, not left held by the timed-out
        # send: it must not be locked, and a fresh notify (capture_session's
        # flush, since _pending_notify is set) must complete immediately
        # rather than blocking behind a wedged holder.
        assert not _descriptions._notify_lock.locked()
        reconnected = MagicMock(spec=ServerSession)
        reconnected.send_tool_list_changed = AsyncMock()
        await asyncio.wait_for(_descriptions.capture_session(reconnected), timeout=5.0)

        reconnected.send_tool_list_changed.assert_awaited_once()
        assert not _descriptions._pending_notify
