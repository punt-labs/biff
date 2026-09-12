"""Tests for dynamic tool description updates and inbox polling."""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
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
from biff.server.state import CompanionSession, ServerState, create_state
from biff.server.tools import _descriptions
from biff.server.tools._descriptions import (
    _READ_MESSAGES_BASE,
    MAX_UNREAD_COUNT,
    SubscriptionBinding,
    _active_tick,
    _CompanionSubs,
    _InboxPokeGate,
    _reconcile_companion_subs,
    _reconcile_inbox_notify_sub,
    _reconcile_talk_sub,
    _sync_unread_file,
    _talk_description,
    _write_unread_file,
    nap_interval_for,
    poll_inbox,
    refresh_read_messages,
    subscribe_companion_talk,
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

    async def test_includes_this_processs_own_pid(self, tmp_path: Path) -> None:
        """``unread-nudge.sh`` uses ``server_pid`` to detect when a
        ``.json`` was written by a different server process than whichever
        process last updated its ``.nudged`` sidecar (PID reuse after an
        unclean kill, or a same-PID subprocess restart) — Bugbot finding
        hwquR. The field must be this process's actual ``os.getpid()``,
        not a placeholder.
        """
        path = tmp_path / "unread.json"
        _write_unread_file(
            path,
            UnreadSummary(count=1),
            repo_name=_TEST_REPO,
            user="kai",
            tty_name="tty1",
            biff_enabled=True,
        )
        data = json.loads(path.read_text())
        assert data["server_pid"] == os.getpid()

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

    async def test_sync_with_no_summary_still_includes_companion_count(
        self, tmp_path: Path
    ) -> None:
        """``_sync_unread_file(state)`` with no ``summary=`` kwarg — the
        shape the display-queue rotation path calls it with — must still
        report the combined primary+companion total, not just the
        primary session's own count.

        Regression: a caller that omits ``summary=`` used to fall back to
        fetching only ``state.session_key``'s count, silently dropping
        the companion's half of the total from the status file on every
        queue rotation, even though ``refresh_read_messages`` shows the
        combined total in the tool description at the same moment.
        """
        companion = CompanionSession(
            user="jfreeman", display_name="Jim", kind="human", tty="bbbb0001"
        )
        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            unread_path=tmp_path / "unread.json",
            companion=companion,
        )
        await state.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="for kai")
        )
        assert state.companion_session_key is not None
        await state.relay.deliver(
            Message(
                from_user="eric",
                to_user=state.companion_session_key,
                body="for the human",
            )
        )

        await _sync_unread_file(state)  # no summary= — the rotation call shape

        assert state.unread_path is not None
        data = json.loads(state.unread_path.read_text())
        assert data["count"] == 2  # 1 primary + 1 companion, not just 1


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


class TestSleepOrWakeLostWakeRace:
    """A ``wake_event.set()`` landing in the narrow window between
    ``asyncio.wait()``'s internal timeout decision and this function's own
    ``wake_event.clear()`` must not be silently erased.

    ``asyncio.wait()``'s timeout resolution and a NATS callback's
    ``wake_event.set()`` are two independent events that can interleave in
    either order; if ``done`` came back empty (the wait-task lost that
    narrow race) while the event was, in fact, set, trusting ``done`` alone
    reports TIMEOUT instead of EVENT. At a disabled poll interval
    (``interval<=0``), ``poll_inbox`` treats TIMEOUT as "skip tick work" —
    so the poke is lost until the next fallback tick, not merely delayed.
    """

    async def test_event_set_despite_empty_done_still_reports_event(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wake_event = asyncio.Event()

        async def _fake_wait(
            waiters: list[asyncio.Future[object]],
            *,
            timeout: float,
            return_when: str,
        ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
            del waiters, timeout, return_when
            # Simulate the race directly: asyncio.wait() decided "nothing
            # completed" (empty done) in the same window a wake_event.set()
            # landed — reproducible without depending on real scheduler
            # timing.
            wake_event.set()
            return set(), set()

        monkeypatch.setattr(asyncio, "wait", _fake_wait)

        outcome = await _descriptions._sleep_or_wake(
            interval=1000.0, shutdown=None, wake_event=wake_event
        )

        assert outcome is _descriptions._WakeOutcome.EVENT
        assert not wake_event.is_set()  # still cleared before returning

    async def test_truly_empty_done_and_unset_event_reports_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ordinary TIMEOUT path must still work — this fix must not
        turn every wait into a false EVENT."""
        wake_event = asyncio.Event()

        async def _fake_wait(
            waiters: list[asyncio.Future[object]],
            *,
            timeout: float,
            return_when: str,
        ) -> tuple[set[asyncio.Future[object]], set[asyncio.Future[object]]]:
            del waiters, timeout, return_when
            return set(), set()

        monkeypatch.setattr(asyncio, "wait", _fake_wait)

        outcome = await _descriptions._sleep_or_wake(
            interval=1000.0, shutdown=None, wake_event=wake_event
        )

        assert outcome is _descriptions._WakeOutcome.TIMEOUT


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

    async def test_local_relay_disabled_interval_still_detects_new_message(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``LocalRelay`` session with polling disabled must still detect
        new mail via the fallback tick, not go permanently silent.

        LocalRelay has no push mechanism of any kind — the disabled-interval
        skip that is safe for a NATS-backed relay (push covers detection
        instead) would otherwise disable ``/write`` detection for the rest
        of the session's life, not merely delay it, since nothing else
        would ever set ``wake_event`` (Bugbot finding hwr7W).
        """
        assert not isinstance(state_with_path.relay, NatsRelay)
        monkeypatch.setattr(
            _descriptions, "_DISABLED_POLLER_FALLBACK_INTERVAL", self._FAST_INTERVAL
        )
        mcp = create_server(state_with_path)
        task = asyncio.create_task(poll_inbox(mcp, state_with_path, interval=0))
        await asyncio.sleep(self._FAST_INTERVAL * 3)

        await state_with_path.relay.deliver(
            Message(from_user="eric", to_user=_KAI_SESSION, body="still here?")
        )
        await asyncio.sleep(self._FAST_INTERVAL * 5)
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
            state: ServerState,
            latch: TalkNotifyLatch,
            gate: _InboxPokeGate,
            wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            del state, latch, gate, wake_event
            nonlocal calls
            calls += 1
            # LocalRelay has no generation, so _relay_generation is 0 — bind the
            # SUB at 0 so the reconcile leaves it alone once it succeeds.
            return None if calls == 1 else SubscriptionBinding(fake_sub, 0)

        monkeypatch.setattr(_descriptions, "subscribe_talk", flaky_subscribe)
        mcp = create_server(state_with_path)
        await self._run_poller(mcp, state_with_path, cycles=8)
        assert calls >= 2  # retried past the initial None
        # Establishing the subscription is proven by its clean teardown on exit.
        fake_sub.unsubscribe.assert_awaited_once()

    async def test_cancellation_between_initial_subscribes_unsubscribes_talk(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cancellation landing between the two initial subscribe() calls
        must not leak the already-established talk SUB.

        Before this fix, both initial subscribes ran BEFORE the
        ``try/finally`` that unsubscribes on exit. A cancellation arriving
        after ``subscribe_talk()`` returned but while
        ``subscribe_inbox_notify()``'s own await was still in flight escaped
        the function before ``talk_sub`` was ever bound inside the
        protected block, so the finally's teardown loop never saw it and
        the live SUB was orphaned.
        """
        talk_unsubscribe = AsyncMock()
        talk_binding = SubscriptionBinding(MagicMock(unsubscribe=talk_unsubscribe), 0)

        async def _fake_subscribe_talk(
            state: ServerState,
            latch: TalkNotifyLatch,
            gate: _InboxPokeGate,
            wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            del state, latch, gate, wake_event
            return talk_binding

        inbox_subscribe_started = asyncio.Event()

        async def _hanging_subscribe_inbox_notify(
            state: ServerState,
            latch: TalkNotifyLatch,
            gate: _InboxPokeGate,
            wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del state, latch, gate, wake_event, user
            inbox_subscribe_started.set()
            await asyncio.sleep(5.0)  # never resolves before the cancellation
            msg = "unreachable — cancellation must fire first"
            raise AssertionError(msg)

        monkeypatch.setattr(_descriptions, "subscribe_talk", _fake_subscribe_talk)
        monkeypatch.setattr(
            _descriptions, "subscribe_inbox_notify", _hanging_subscribe_inbox_notify
        )

        mcp = create_server(state_with_path)
        task = asyncio.create_task(
            poll_inbox(mcp, state_with_path, interval=self._FAST_INTERVAL)
        )
        await inbox_subscribe_started.wait()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        talk_unsubscribe.assert_awaited_once()

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
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding:
            events.append(("subscribe", gen[0]))
            return SubscriptionBinding(AsyncMock(), gen[0])

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
            *,
            gate: _InboxPokeGate,
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            del gate
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
        expensive relay poll is skipped — proven by checking, at the exact
        moment the rebind happens, that ``_safe_tick`` had not yet run (the
        cheap-nap skip held for that same tick).

        ``_reconcile_always_on_sub`` now also wakes the tracker on any
        rebind (Bugbot finding hwr7z) — deliberately: a SUB re-established
        after an orphaned window may have missed a poke with no replay, so
        the very next tick must recompute for real rather than staying
        napped indefinitely. That un-nap is expected to let subsequent
        ticks run ``_safe_tick`` too; this test only pins the rebinding
        tick itself, not what happens afterward.
        """
        events: list[tuple[str, int]] = []
        tick_calls = [0]
        subscribe_calls = [0]
        tick_calls_at_rebind: list[int] = []
        rebind_done = asyncio.Event()

        def _gen(_state: ServerState) -> int:
            return 1  # a background swap advanced the generation past the SUB

        async def fake_subscribe(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding:
            subscribe_calls[0] += 1
            # First call is the startup bind at the pre-swap generation (0, now
            # stale); the cheap-nap reconcile rebinds to the live generation (1).
            bound = 0 if subscribe_calls[0] == 1 else 1
            events.append(("subscribe", bound))
            if bound == 1:
                tick_calls_at_rebind.append(tick_calls[0])
                rebind_done.set()
            return SubscriptionBinding(AsyncMock(), bound)

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
            *,
            gate: _InboxPokeGate,
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            del gate
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
        await asyncio.wait_for(rebind_done.wait(), timeout=2.0)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert ("subscribe", 1) in events  # rebound on the cheap nap tick
        assert tick_calls_at_rebind == [0]  # _safe_tick had not yet run at rebind time

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
            return_value="biff-dev._test-server.notify.kai"
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
        assert any(s.endswith("notify.kai") and "talk" not in s for s in subjects)
        talk_handle.unsubscribe.assert_awaited_once()
        inbox_handle.unsubscribe.assert_awaited_once()

    async def test_companion_registered_after_start_still_gets_a_sub(
        self, tmp_path: Path
    ) -> None:
        """A companion set AFTER ``poll_inbox`` starts still opens its own
        inbox-notify SUB — production always hits this path: the
        heartbeat loop's ``_poll_companion_registration`` sets
        ``state.companion`` (via ``object.__setattr__`` on the frozen
        ``ServerState``) well after the poller task is already running,
        never before it. A one-shot check for ``state.companion`` made
        only before the tick loop starts would permanently miss this —
        the per-tick reconcile must pick it up lazily.
        """
        nc = AsyncMock()
        subjects: list[str] = []

        async def fake_subscribe(subject: str, *, cb: object) -> AsyncMock:
            del cb
            subjects.append(subject)
            return AsyncMock()

        nc.subscribe = AsyncMock(side_effect=fake_subscribe)

        def _inbox_notify_subject(repo: str, user: str) -> str:
            return f"biff.{repo}.notify.{user}"

        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.inbox_notify_subject = MagicMock(side_effect=_inbox_notify_subject)
        relay.get_wall = AsyncMock(return_value=None)
        relay.get_unread_summary = AsyncMock(return_value=UnreadSummary(count=0))

        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
            # companion intentionally omitted — None at poller start,
            # matching production.
        )
        mcp = create_server(state)
        task = asyncio.create_task(poll_inbox(mcp, state, interval=self._FAST_INTERVAL))
        await asyncio.sleep(self._FAST_INTERVAL * 3)

        # The heartbeat loop's own mutation shape (ServerState is frozen).
        object.__setattr__(
            state,
            "companion",
            CompanionSession(
                user="jfreeman", display_name="Jim", kind="human", tty="bbbb0001"
            ),
        )
        await asyncio.sleep(self._FAST_INTERVAL * 5)

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert f"biff.{_TEST_REPO}.notify.jfreeman" in subjects

    async def test_disabled_interval_recovers_from_a_client_discard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """At ``interval<=0``, a client discard must not strand every
        always-on SUB forever.

        Waiting on ``wake_event`` alone with no timeout has a real
        liveness hole: a wedge teardown (``_force_reconnect``) or give-up
        close (``_on_closed``) orphans every SUB on the dead client, and
        only the reconcile step — which runs after ``_sleep_or_wake``
        returns — can rebind them. With no live SUB, no poke can ever
        arrive to end that wait, so reconcile never runs and the SUBs
        stay stranded permanently. ``_DISABLED_POLLER_FALLBACK_INTERVAL``
        (shrunk here for test speed) is the liveness backstop that
        breaks this: it bounds the wait even at ``interval<=0``, so
        reconcile still gets scheduled with zero poke traffic.
        """
        monkeypatch.setattr(
            _descriptions, "_DISABLED_POLLER_FALLBACK_INTERVAL", self._FAST_INTERVAL
        )
        nc = AsyncMock()
        subscribe_calls = 0

        async def fake_subscribe(subject: str, *, cb: object) -> AsyncMock:
            del subject, cb
            nonlocal subscribe_calls
            subscribe_calls += 1
            return AsyncMock()

        nc.subscribe = AsyncMock(side_effect=fake_subscribe)

        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.inbox_notify_subject = MagicMock(
            return_value="biff._test-server.notify.kai"
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
        task = asyncio.create_task(poll_inbox(mcp, state, interval=0))
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        assert subscribe_calls == 2  # talk + inbox-notify, startup bind

        # A wedge teardown/give-up close, independent of this poller —
        # exactly what the heartbeat loop's own _force_reconnect does.
        relay.connection_generation = 1
        await asyncio.sleep(self._FAST_INTERVAL * 10)

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert subscribe_calls > 2  # reconcile ran and rebound both SUBs

    async def test_disabled_interval_fallback_skips_tick_work_with_no_real_event(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A bare fallback-timeout wake at ``interval<=0`` — no real event,
        no reconcile-triggered rebind, nothing new — must not run the
        periodic tick work (``get_wall``, ``get_unread_summary``). See
        ``test_disabled_interval_reconcile_rebind_forces_a_recompute`` for
        the complementary case where a rebind DOES force one.
        """
        monkeypatch.setattr(
            _descriptions, "_DISABLED_POLLER_FALLBACK_INTERVAL", self._FAST_INTERVAL
        )
        nc = AsyncMock()

        async def fake_subscribe(subject: str, *, cb: object) -> AsyncMock:
            del subject, cb
            return AsyncMock()

        nc.subscribe = AsyncMock(side_effect=fake_subscribe)

        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.inbox_notify_subject = MagicMock(
            return_value="biff._test-server.notify.kai"
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
        task = asyncio.create_task(poll_inbox(mcp, state, interval=0))
        await asyncio.sleep(self._FAST_INTERVAL * 5)

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        relay.get_wall.assert_not_awaited()
        relay.get_unread_summary.assert_not_awaited()

    async def test_disabled_interval_reconcile_rebind_forces_a_recompute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stranded-SUB rebind at ``interval<=0`` must force a real
        recompute, not just rebind silently.

        A SUB orphaned by a wedge teardown and later rebound may have
        missed a poke published while nothing was listening — core NATS
        has no replay. At a disabled interval, nothing else would ever
        trigger ``get_unread_summary`` again after such a rebind, so a
        message that arrived during the gap would stay undetected
        forever, not just delayed (Bugbot finding hwr7z).
        """
        monkeypatch.setattr(
            _descriptions, "_DISABLED_POLLER_FALLBACK_INTERVAL", self._FAST_INTERVAL
        )
        nc = AsyncMock()
        subscribe_calls = 0

        async def fake_subscribe(subject: str, *, cb: object) -> AsyncMock:
            del subject, cb
            nonlocal subscribe_calls
            subscribe_calls += 1
            return AsyncMock()

        nc.subscribe = AsyncMock(side_effect=fake_subscribe)

        relay = MagicMock(spec=NatsRelay)
        relay.get_nc = AsyncMock(return_value=nc)
        relay.connection_generation = 0
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.inbox_notify_subject = MagicMock(
            return_value="biff._test-server.notify.kai"
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
        task = asyncio.create_task(poll_inbox(mcp, state, interval=0))
        await asyncio.sleep(self._FAST_INTERVAL * 3)
        assert subscribe_calls == 2  # talk + inbox-notify, startup bind
        relay.get_wall.assert_not_awaited()  # steady state: nothing to recompute yet

        # A wedge teardown/give-up close, independent of this poller.
        relay.connection_generation = 1
        await asyncio.sleep(self._FAST_INTERVAL * 10)

        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

        assert subscribe_calls > 2  # reconcile ran and rebound both SUBs
        relay.get_wall.assert_awaited()  # the rebind forced a real recompute

    async def test_cheap_nap_tick_reconciles_inbox_notify_generation_bump(
        self, state_with_path: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A cheap nap tick still reconciles the inbox-notify SUB too.

        Mirrors ``test_cheap_nap_tick_reconciles_background_generation_bump``
        for the second always-on SUB the ``SubKind``-indexed family adds: a
        background wedge teardown/redial can orphan the inbox-notify SUB
        during a nap exactly as it can the talk SUB, and both reconciles sit
        in the same unconditional region of the tick loop (after the
        cheap-nap skip), so both must run on a cheap tick alike.
        """
        events: list[tuple[str, int]] = []
        subscribe_calls = [0]

        def _gen(_state: ServerState) -> int:
            return 1  # a background swap advanced the generation past the SUB

        async def fake_subscribe(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding:
            del user
            subscribe_calls[0] += 1
            # First call is the startup bind at the pre-swap generation (0, now
            # stale); the cheap-nap reconcile rebinds to the live generation (1).
            bound = 0 if subscribe_calls[0] == 1 else 1
            events.append(("subscribe", bound))
            return SubscriptionBinding(AsyncMock(), bound)

        async def fake_tick(
            _mcp: FastMCP[ServerState],
            _state: ServerState,
            last_count: int,
            last_wall: tuple[str, str],
            last_talk: tuple[tuple[str, ...], int, str],
            *,
            gate: _InboxPokeGate,
        ) -> tuple[int, tuple[str, str], tuple[tuple[str, ...], int, str]]:
            del gate
            return last_count, last_wall, last_talk

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", fake_subscribe)
        monkeypatch.setattr(_descriptions, "_relay_generation", _gen)
        monkeypatch.setattr(_descriptions, "_safe_tick", fake_tick)

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

    @staticmethod
    def _gate() -> _InboxPokeGate:
        return _InboxPokeGate(backstop_interval=1000.0)

    async def test_no_resubscribe_when_generation_unchanged(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An in-place reconnect keeps the generation, so the SUB is left as-is.

        nats-py replays every SUB on the same client — re-subscribing would
        leak a duplicate.  This is the case the is-None probe gets right by
        accident and the generation check gets right by construction.
        """
        handle = AsyncMock()
        current = SubscriptionBinding(handle, generation=3)
        calls = 0

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            nonlocal calls
            calls += 1
            return SubscriptionBinding(AsyncMock(), 3)

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(3))

        result = await _reconcile_talk_sub(
            state, current, _test_latch(), self._gate(), asyncio.Event()
        )

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
        fresh = SubscriptionBinding(AsyncMock(), 4)

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(4))

        result = await _reconcile_talk_sub(
            state,
            SubscriptionBinding(stale, 3),
            _test_latch(),
            self._gate(),
            asyncio.Event(),
        )

        assert result is fresh
        stale.unsubscribe.assert_awaited_once()

    async def test_subscribes_when_never_established(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A None handle (failed initial subscribe) is retried."""
        fresh = SubscriptionBinding(AsyncMock(), 1)

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(1))

        result = await _reconcile_talk_sub(
            state, None, _test_latch(), self._gate(), asyncio.Event()
        )

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
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            return None

        monkeypatch.setattr(_descriptions, "subscribe_talk", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(5))

        result = await _reconcile_talk_sub(
            state,
            SubscriptionBinding(stale, 2),
            _test_latch(),
            self._gate(),
            asyncio.Event(),
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
        gate = _InboxPokeGate(backstop_interval=1000.0)
        assert gate.claim() is True

    def test_no_recompute_before_backstop_with_no_poke(self) -> None:
        """An unpoked tick within the backstop window does not recompute."""
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute
        assert gate.claim() is False

    def test_poke_forces_recompute_regardless_of_backstop(self) -> None:
        """A marked poke recomputes immediately, even mid-backstop-window."""
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()
        gate.mark()
        assert gate.claim() is True

    def test_backstop_recomputes_with_no_poke(self) -> None:
        """The backstop cadence fires a recompute even with no poke at all.

        This is the dropped-poke insurance: an at-most-once core-NATS poke
        that never arrives still gets picked up within one backstop
        interval instead of stalling forever.
        """
        gate = _InboxPokeGate(backstop_interval=0.0)
        gate.claim()
        assert gate.claim() is True

    def test_claim_clears_poke_and_resets_clock(self) -> None:
        """A successful ``claim()`` clears the poke flag and restarts the clock."""
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.mark()
        assert gate.claim() is True  # consumes both the poke and the initial force
        assert gate.claim() is False

    def test_mark_after_claim_is_preserved_for_next_claim(self) -> None:
        """Ordering property (a): a poke that arrives after ``claim()``
        clears the flag (e.g. mid-refresh, while the caller is awaiting)
        is not lost — it survives to be picked up by the next ``claim()``.

        ``claim()`` clears ``_poked`` synchronously before returning, so
        nothing the caller does with the returned ``True`` (including
        awaiting a slow refresh) can race a concurrent ``mark()``.
        """
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial force; simulates "refresh in flight"
        gate.mark()  # a poke arrives while that refresh is still running
        assert gate.claim() is True  # not clobbered — the next tick sees it

    def test_mark_after_failed_refresh_re_arms_retry(self) -> None:
        """Ordering property (b): re-marking after a failed refresh makes
        the very next ``claim()`` due again, instead of waiting out a
        full backstop interval for a transient failure.
        """
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial force
        # Simulate _active_tick's re-mark-on-failure branch.
        gate.mark()
        assert gate.claim() is True


class TestActiveTickGateOrderingProperties:
    """The two ``_InboxPokeGate`` ordering properties, pinned through the
    real ``_active_tick`` call path rather than by driving the gate
    directly. ``TestInboxPokeGate``'s own tests call ``gate.mark()``/
    ``gate.claim()`` from the test body and never invoke ``_active_tick``
    at all — so they cannot fail if ``_active_tick``'s own
    re-mark-on-failure branch (``if not ok: gate.mark()``) is broken or
    inverted; only these tests, which drive the real branch, can.
    """

    @staticmethod
    def _nats_state(tmp_path: Path) -> tuple[ServerState, MagicMock]:
        relay = MagicMock(spec=NatsRelay)
        relay.get_wall = AsyncMock(return_value=None)
        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )
        return state, relay

    async def test_failed_refresh_re_marks_and_next_tick_retries(
        self, tmp_path: Path
    ) -> None:
        """Property (b), through ``_active_tick`` itself: a fetch failure
        re-marks the gate, so the very next tick retries instead of
        waiting out a 1000s backstop.
        """
        state, relay = self._nats_state(tmp_path)
        mcp = create_server(state)
        gate = _InboxPokeGate(backstop_interval=1000.0)

        calls = 0

        async def _flaky_get_unread_summary(_key: str) -> UnreadSummary:
            nonlocal calls
            calls += 1
            if calls == 1:
                msg = "nats: timeout"
                raise TimeoutError(msg)
            return UnreadSummary(count=1)

        relay.get_unread_summary = AsyncMock(side_effect=_flaky_get_unread_summary)

        # Tick 1: the initial forced claim() fires and the fetch fails —
        # _active_tick's own re-mark-on-failure branch must run.
        await _active_tick(mcp, state, -1, ("", ""), ((), -1, ""), gate=gate)
        assert calls == 1

        # Tick 2: with a 1000s backstop, only a re-mark from tick 1 can
        # make claim() due again — a broken or inverted re-mark branch
        # leaves get_unread_summary uncalled here.
        await _active_tick(mcp, state, -1, ("", ""), ((), -1, ""), gate=gate)
        assert calls == 2

    async def test_poke_arriving_mid_refresh_survives_to_next_tick(
        self, tmp_path: Path
    ) -> None:
        """Property (a), through ``_active_tick`` itself: a poke that
        arrives while the tick's own refresh is in flight (``gate.mark()``
        called from inside the ``get_unread_summary`` side effect,
        standing in for a concurrent inbox-notify callback) is not
        clobbered by ``claim()``'s earlier clear — it survives to be
        claimed on the next tick.
        """
        state, relay = self._nats_state(tmp_path)
        mcp = create_server(state)
        gate = _InboxPokeGate(backstop_interval=1000.0)

        calls = 0

        async def _poke_mid_refresh(_key: str) -> UnreadSummary:
            nonlocal calls
            calls += 1
            if calls == 1:
                # A poke lands while this fetch is in flight — claim()
                # already cleared _poked before this coroutine started.
                gate.mark()
            return UnreadSummary(count=1)

        relay.get_unread_summary = AsyncMock(side_effect=_poke_mid_refresh)

        await _active_tick(mcp, state, -1, ("", ""), ((), -1, ""), gate=gate)
        assert calls == 1

        # Tick 2: only the mid-refresh mark from tick 1 can make claim()
        # due again with a 1000s backstop.
        await _active_tick(mcp, state, -1, ("", ""), ((), -1, ""), gate=gate)
        assert calls == 2


class TestTargetedMessageMarksInboxGate:
    """A targeted (``user:tty``) message's wake poke must mark the shared
    inbox gate, not just wake the poller — the regression this pins
    permanently. Drives the exact frame ``_publish_talk_notification``
    publishes for a targeted message through the real ``subscribe_talk``
    callback (mirroring the original repro's setup exactly), then asserts
    the FIXED behavior: the next active tick recomputes and the
    description shows the unread count. A test that called ``gate.mark()``
    by hand instead would pass on both the broken and the fixed code — it
    would prove nothing about whether the callback itself marks the gate.
    """

    @staticmethod
    def _nats_state(tmp_path: Path, nc: AsyncMock) -> ServerState:
        relay = MagicMock(spec=NatsRelay)
        relay.connection_generation = 1
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        relay.get_unread_summary = AsyncMock(return_value=UnreadSummary(count=1))
        relay.get_wall = AsyncMock(return_value=None)
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )

    async def test_targeted_wake_poke_recomputes_on_next_tick(
        self, tmp_path: Path
    ) -> None:
        nc = AsyncMock()
        state = self._nats_state(tmp_path, nc)
        mcp = create_server(state)

        gate = _InboxPokeGate(backstop_interval=1000.0)
        wake_event = asyncio.Event()
        gate.claim()  # steady state: consume the initial forced recompute

        # Establish the talk SUB and drive the exact frame deliver() publishes
        # for a targeted message (_publish_talk_notification's payload: no
        # "type" key -> classified as a wake poke, not a modeled talk frame).
        await subscribe_talk(
            state,
            TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER)),
            gate,
            wake_event,
        )
        on_talk = nc.subscribe.call_args.kwargs["cb"]
        frame = json.dumps(
            {
                "from": "eric",
                "body": "ping",
                "to_key": "kai:tty1",
                "from_key": "eric:tty2",
            }
        ).encode()
        msg = MagicMock()
        msg.data = frame
        await on_talk(msg)

        # The callback both wakes the poller and marks the gate.
        assert state.activity.napping is False
        assert wake_event.is_set()

        await _active_tick(mcp, state, -1, ("", ""), ((), -1, ""), gate=gate)

        tool = await mcp.get_tool("read_messages")
        assert tool is not None
        assert "1 unread" in (tool.description or "")


class TestTalkCallbackPreservesWireTypes:
    """The talk-SUB callback must not stringify decoded JSON values before
    ``TalkNotification.from_payload`` type-guards them.

    Before this fix, the callback's dict comprehension ran ``str(v)`` on
    every decoded value, so by the time ``from_payload``'s ``_trusted()``
    checked ``isinstance(value, str)`` every value already *was* a str —
    the guard against non-str JSON types (forged ``null``, numbers, nested
    structures) was unreachable. A forged ``{"body": null}`` became
    ``str(None)`` == ``"None"``, which passed the (defeated) guard and
    rendered as if the sender had actually typed the word "None".
    """

    async def test_forged_null_body_does_not_render_as_the_string_none(
        self, tmp_path: Path
    ) -> None:
        nc = AsyncMock()
        relay = MagicMock(spec=NatsRelay)
        relay.connection_generation = 1
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.talk.notify.kai:tty1")
        state = create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
        )
        gate = _InboxPokeGate(backstop_interval=1000.0)
        wake_event = asyncio.Event()

        await subscribe_talk(
            state,
            TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER)),
            gate,
            wake_event,
        )
        on_talk = nc.subscribe.call_args.kwargs["cb"]

        # A forged frame: "body" is JSON null, not a string. "type": "message"
        # makes this a modeled frame (not a wake poke) so it is actually
        # enqueued and observable via drain_idle().
        msg = MagicMock()
        msg.data = json.dumps(
            {
                "type": "message",
                "from": "eric",
                "body": None,
                "to_key": "kai:tty1",
                "from_key": "eric:tty2",
            }
        ).encode()
        await on_talk(msg)

        drained = state.talk.drain_idle()
        assert len(drained) == 1
        # The documented default for a non-str value (empty string), never
        # the stringified "None".
        assert drained[0].nbody == ""


class TestReconcileCompanionSubs:
    """A newly (re)established companion inbox-notify SUB must mark the
    gate so pre-existing broadcast mail is not stranded.

    Core NATS has no replay: a broadcast poke published to the companion's
    inbox before this process ever subscribed to it is gone forever. Only
    ``stream_info`` (a real recompute) can discover mail that arrived
    before the SUB existed — so the very first successful bind (or a retry
    that finally succeeds after prior failures) must force that recompute,
    the same way ``_InboxPokeGate.__init__`` starting poked already forces
    it for this session's OWN inbox at poller startup.
    """

    @staticmethod
    def _state(tmp_path: Path) -> ServerState:
        companion = CompanionSession(
            user="jfreeman", display_name="Jim", kind="human", tty="bbbb0001"
        )
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            companion=companion,
        )

    async def test_first_bind_marks_the_gate_and_wakes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        state = self._state(tmp_path)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        fresh = SubscriptionBinding(AsyncMock(), 0)

        async def _fake_subscribe_inbox_notify(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del user
            return fresh

        monkeypatch.setattr(
            _descriptions, "subscribe_inbox_notify", _fake_subscribe_inbox_notify
        )

        result = await _reconcile_companion_subs(
            state, _CompanionSubs(None, None, None, None), gate, wake_event
        )

        assert result.inbox_sub is fresh
        assert wake_event.is_set()
        assert gate.claim() is True  # the first bind forced a recompute

    async def test_retry_that_finally_succeeds_marks_the_gate(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The same catch-up applies when a prior tick's subscribe failed
        (``inbox_sub`` still ``None``) and this tick's retry succeeds."""
        state = self._state(tmp_path)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()
        wake_event = asyncio.Event()

        fresh = SubscriptionBinding(AsyncMock(), 0)

        async def _fake_subscribe_inbox_notify(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del user
            return fresh

        monkeypatch.setattr(
            _descriptions, "subscribe_inbox_notify", _fake_subscribe_inbox_notify
        )

        # A previously-failed attempt already created the latch (and
        # recorded the companion's own identity), but inbox_sub is still
        # None — this reconcile pass is the retry for the SAME identity.
        assert state.companion is not None
        previously_failed = _CompanionSubs(
            _test_latch(), None, None, None, state.companion.session_key
        )

        result = await _reconcile_companion_subs(
            state, previously_failed, gate, wake_event
        )

        assert result.inbox_sub is fresh
        assert gate.claim() is True

    async def test_steady_state_does_not_mark_the_gate(self, tmp_path: Path) -> None:
        """Once bound, an ordinary no-op reconcile (generation unchanged,
        identity unchanged) must not spuriously mark the gate on every
        tick."""
        state = self._state(tmp_path)
        assert state.companion is not None
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()
        wake_event = asyncio.Event()

        already_bound = SubscriptionBinding(AsyncMock(), 0)
        steady = _CompanionSubs(
            _test_latch(),
            already_bound,
            _test_latch(),
            SubscriptionBinding(AsyncMock(), 0),
            state.companion.session_key,
        )

        result = await _reconcile_companion_subs(state, steady, gate, wake_event)

        assert result.inbox_sub is already_bound
        assert gate.claim() is False
        assert not wake_event.is_set()

    async def test_companion_rollback_to_none_unsubscribes_old_subs(
        self, tmp_path: Path
    ) -> None:
        """``state.companion`` rolling back to ``None`` with the connection
        generation unchanged must unsubscribe the old identity's SUBs, not
        leave them bound forever.

        A connection-generation-only reconcile would never notice this —
        the SUBs are still live on the still-current client, so
        ``_reconcile_always_on_sub`` alone sees nothing to do. Without
        identity tracking, this process keeps waking for a companion that
        is no longer relevant (Bugbot finding hwr78).
        """
        state = self._state(tmp_path)
        assert state.companion is not None
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        old_inbox_handle = AsyncMock()
        old_talk_handle = AsyncMock()
        bound = _CompanionSubs(
            _test_latch(),
            SubscriptionBinding(old_inbox_handle, 0),
            _test_latch(),
            SubscriptionBinding(old_talk_handle, 0),
            state.companion.session_key,
        )

        object.__setattr__(state, "companion", None)
        result = await _reconcile_companion_subs(state, bound, gate, wake_event)

        old_inbox_handle.unsubscribe.assert_awaited_once()
        old_talk_handle.unsubscribe.assert_awaited_once()
        assert result.inbox_sub is None
        assert result.talk_sub is None
        assert result.identity is None
        # The drop itself is an unread-state change (the companion's
        # contribution vanishes from the next combined-count recompute)
        # with no fresh SUB bind left to mark the gate on our behalf.
        assert wake_event.is_set()
        assert gate.claim() is True

    async def test_companion_identity_change_unsubscribes_old_and_binds_new(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A different companion taking over the role (same connection
        generation) must unsubscribe the old identity's SUBs and bind the
        new identity's own — not silently keep listening on the stale
        subject forever, and not silently skip binding the new one because
        ``_reconcile_always_on_sub`` already sees a live SUB (Bugbot
        finding hwr78).
        """
        state = self._state(tmp_path)
        assert state.companion is not None
        old_identity = state.companion.session_key
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        old_inbox_handle = AsyncMock()
        old_talk_handle = AsyncMock()
        bound = _CompanionSubs(
            _test_latch(),
            SubscriptionBinding(old_inbox_handle, 0),
            _test_latch(),
            SubscriptionBinding(old_talk_handle, 0),
            old_identity,
        )

        new_companion = CompanionSession(
            user="newperson", display_name="New", kind="human", tty="cccc0002"
        )
        object.__setattr__(state, "companion", new_companion)

        fresh_inbox = SubscriptionBinding(AsyncMock(), 0)
        fresh_talk = SubscriptionBinding(AsyncMock(), 0)
        bind_users: list[str] = []

        async def _fake_subscribe_inbox_notify(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            bind_users.append(user)
            return fresh_inbox

        async def _fake_subscribe_companion_talk(
            _state: ServerState,
            companion: CompanionSession,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
        ) -> SubscriptionBinding | None:
            bind_users.append(companion.user)
            return fresh_talk

        monkeypatch.setattr(
            _descriptions, "subscribe_inbox_notify", _fake_subscribe_inbox_notify
        )
        monkeypatch.setattr(
            _descriptions, "subscribe_companion_talk", _fake_subscribe_companion_talk
        )

        result = await _reconcile_companion_subs(state, bound, gate, wake_event)

        old_inbox_handle.unsubscribe.assert_awaited_once()
        old_talk_handle.unsubscribe.assert_awaited_once()
        assert bind_users == ["newperson", "newperson"]
        assert result.inbox_sub is fresh_inbox
        assert result.talk_sub is fresh_talk
        assert result.identity == new_companion.session_key
        assert result.identity != old_identity
        # An identity CHANGE (not a drop) is covered by the normal
        # fresh-bind path -- _reconcile_always_on_sub's own mark+wake on
        # the new identity's successful inbox-notify subscribe, not the
        # drop-specific handling above -- verified explicitly here rather
        # than assumed.
        assert wake_event.is_set()
        assert gate.claim() is True


class TestCompanionTalkCallbackGatesOnWakePoke:
    """``subscribe_companion_talk``'s callback must mark the shared inbox
    gate only for a wake poke, not for every frame on the companion's talk
    subject.

    Before this fix, the callback unconditionally called ``gate.mark()``
    for every frame, including a genuine talk message/invite/end/withdraw
    addressed to the companion — forcing a spurious ``stream_info``
    recompute on every real conversation turn. ``subscribe_talk`` already
    applies the wake-poke classification to this session's own talk
    subject (DES-062's local-review fix round); the companion's subject
    deserves the same discipline.
    """

    @staticmethod
    def _state(tmp_path: Path, nc: AsyncMock) -> ServerState:
        relay = MagicMock(spec=NatsRelay)
        relay.connection_generation = 1
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(
            return_value="biff.talk.notify.jfreeman:bbbb0001"
        )
        companion = CompanionSession(
            user="jfreeman", display_name="Jim", kind="human", tty="bbbb0001"
        )
        return create_state(
            BiffConfig(user="kai", repo_name=_TEST_REPO),
            tmp_path,
            tty="tty1",
            hostname="test-host",
            pwd="/test",
            relay=relay,
            companion=companion,
        )

    async def test_genuine_talk_frame_wakes_but_does_not_mark_the_gate(
        self, tmp_path: Path
    ) -> None:
        nc = AsyncMock()
        state = self._state(tmp_path, nc)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        assert state.companion is not None
        await subscribe_companion_talk(
            state, state.companion, _test_latch(), gate, wake_event
        )
        on_companion_talk = nc.subscribe.call_args.kwargs["cb"]

        msg = MagicMock()
        msg.data = json.dumps(
            {
                "type": "message",
                "from": "eric",
                "body": "hello",
                "to_key": "jfreeman:bbbb0001",
                "from_key": "eric:tty2",
            }
        ).encode()
        await on_companion_talk(msg)

        assert wake_event.is_set()  # presence is still active
        assert gate.claim() is False  # but no spurious unread recompute

    async def test_wake_poke_marks_the_gate(self, tmp_path: Path) -> None:
        nc = AsyncMock()
        state = self._state(tmp_path, nc)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        assert state.companion is not None
        await subscribe_companion_talk(
            state, state.companion, _test_latch(), gate, wake_event
        )
        on_companion_talk = nc.subscribe.call_args.kwargs["cb"]

        # A /write mail notification riding the talk subject: no "type" key.
        msg = MagicMock()
        msg.data = json.dumps(
            {"from": "eric", "body": "for the human", "to_key": "jfreeman:bbbb0001"}
        ).encode()
        await on_companion_talk(msg)

        assert wake_event.is_set()
        assert gate.claim() is True  # the poke is owed a recompute

    async def test_legacy_bare_wake_marks_the_gate(self, tmp_path: Path) -> None:
        nc = AsyncMock()
        state = self._state(tmp_path, nc)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute — steady state
        wake_event = asyncio.Event()

        assert state.companion is not None
        await subscribe_companion_talk(
            state, state.companion, _test_latch(), gate, wake_event
        )
        on_companion_talk = nc.subscribe.call_args.kwargs["cb"]

        msg = MagicMock()
        msg.data = b"1"  # legacy non-dict payload
        await on_companion_talk(msg)

        assert wake_event.is_set()
        assert gate.claim() is True


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
        gate = _InboxPokeGate(backstop_interval=1000.0)
        wake_event = asyncio.Event()

        with caplog.at_level(logging.DEBUG, logger=_TALK_LOGGER):
            # onset — WARNING
            assert await subscribe_talk(state, latch, gate, wake_event) is None
            # retry — DEBUG
            assert await subscribe_talk(state, latch, gate, wake_event) is None

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1  # not one per tick

            relay.get_nc = AsyncMock(return_value=AsyncMock())  # NATS recovers
            sub = await subscribe_talk(state, latch, gate, wake_event)

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
            return_value="biff-dev._test-server.notify.kai"
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
        gate = _InboxPokeGate(backstop_interval=1000.0)
        result = await subscribe_inbox_notify(
            state, _test_latch(), gate, asyncio.Event(), user="kai"
        )
        assert result is None

    async def test_subscribes_on_the_repo_scoped_subject(self, tmp_path: Path) -> None:
        state, relay = self._nats_state(tmp_path)
        relay.get_nc = AsyncMock(return_value=AsyncMock())
        gate = _InboxPokeGate(backstop_interval=1000.0)
        sub = await subscribe_inbox_notify(
            state, _test_latch(), gate, asyncio.Event(), user="kai"
        )
        assert sub is not None
        assert sub.generation == 1
        relay.inbox_notify_subject.assert_called_once_with("_test-server", "kai")

    async def test_subscribes_on_a_different_users_subject_for_companion(
        self, tmp_path: Path
    ) -> None:
        """The *user* argument, not always ``state.config.user``, decides
        the subject — the companion binding passes the companion's user
        so its broadcast pokes land on a subject this session actually
        subscribes to.
        """
        state, relay = self._nats_state(tmp_path)
        relay.get_nc = AsyncMock(return_value=AsyncMock())
        gate = _InboxPokeGate(backstop_interval=1000.0)
        sub = await subscribe_inbox_notify(
            state, _test_latch(), gate, asyncio.Event(), user="eric"
        )
        assert sub is not None
        relay.inbox_notify_subject.assert_called_once_with("_test-server", "eric")

    async def test_callback_marks_gate_and_wakes(self, tmp_path: Path) -> None:
        """The callback marks the poke gate and wakes the poller — nothing else."""
        state, relay = self._nats_state(tmp_path)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        gate.claim()  # consume the initial forced recompute
        wake_event = asyncio.Event()
        state.activity.enter_nap()

        await subscribe_inbox_notify(state, _test_latch(), gate, wake_event, user="kai")
        callback = nc.subscribe.call_args.kwargs["cb"]
        await callback(object())

        assert gate.claim() is True
        assert state.activity.napping is False  # wake() exited napping
        assert wake_event.is_set()

    async def test_failure_then_recovery_logs_once_each(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        state, relay = self._nats_state(tmp_path)
        gate = _InboxPokeGate(backstop_interval=1000.0)
        wake_event = asyncio.Event()
        latch = TalkNotifyLatch.for_resubscribe(logging.getLogger(_TALK_LOGGER))

        with caplog.at_level(logging.DEBUG, logger=_TALK_LOGGER):
            assert (
                await subscribe_inbox_notify(state, latch, gate, wake_event, user="kai")
                is None
            )
            assert (
                await subscribe_inbox_notify(state, latch, gate, wake_event, user="kai")
                is None
            )

            warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
            assert len(warnings) == 1  # not one per tick

            relay.get_nc = AsyncMock(return_value=AsyncMock())  # NATS recovers
            sub = await subscribe_inbox_notify(
                state, latch, gate, wake_event, user="kai"
            )

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

    @staticmethod
    def _gate() -> _InboxPokeGate:
        return _InboxPokeGate(backstop_interval=1000.0)

    async def test_no_resubscribe_when_generation_unchanged(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        handle = AsyncMock()
        current = SubscriptionBinding(handle, generation=3)
        calls = 0

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del user
            nonlocal calls
            calls += 1
            return SubscriptionBinding(AsyncMock(), 3)

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(3))

        result = await _reconcile_inbox_notify_sub(
            state,
            current,
            _test_latch(),
            self._gate(),
            asyncio.Event(),
            user="kai",
        )

        assert result is current
        assert calls == 0
        handle.unsubscribe.assert_not_awaited()

    async def test_resubscribes_when_client_replaced(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stale = AsyncMock()
        fresh = SubscriptionBinding(AsyncMock(), 4)

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del user
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(4))

        result = await _reconcile_inbox_notify_sub(
            state,
            SubscriptionBinding(stale, 3),
            _test_latch(),
            self._gate(),
            asyncio.Event(),
            user="kai",
        )

        assert result is fresh
        stale.unsubscribe.assert_awaited_once()

    async def test_subscribes_when_never_established(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fresh = SubscriptionBinding(AsyncMock(), 1)

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            del user
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(1))

        result = await _reconcile_inbox_notify_sub(
            state, None, _test_latch(), self._gate(), asyncio.Event(), user="kai"
        )

        assert result is fresh

    async def test_reconciles_the_companion_binding_independently(
        self, state: ServerState, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The companion's SUB is reconciled by the SAME helper,
        parameterized on ``user`` — proving one generic reconcile serves
        both the own-user and companion-user bindings without a second
        code path.
        """
        stale = AsyncMock()
        fresh = SubscriptionBinding(AsyncMock(), 4)
        seen_users: list[str] = []

        async def _sub(
            _state: ServerState,
            _latch: TalkNotifyLatch,
            _gate: _InboxPokeGate,
            _wake_event: asyncio.Event,
            *,
            user: str,
        ) -> SubscriptionBinding | None:
            seen_users.append(user)
            return fresh

        monkeypatch.setattr(_descriptions, "subscribe_inbox_notify", _sub)
        monkeypatch.setattr(_descriptions, "_relay_generation", _fixed_generation(4))

        result = await _reconcile_inbox_notify_sub(
            state,
            SubscriptionBinding(stale, 3),
            _test_latch(),
            self._gate(),
            asyncio.Event(),
            user="eric",
        )

        assert result is fresh
        assert seen_users == ["eric"]
        stale.unsubscribe.assert_awaited_once()


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

    async def test_drop_then_suspenders_success_flushes(
        self, state: ServerState
    ) -> None:
        """A suspenders send failure followed by a LATER SUCCESSFUL suspenders
        send flushes the earlier drop too (biff-6vuv).

        ``PollTickNotifyOk`` now clears ``pendingNotify`` unconditionally,
        the same argument ``NotifyBelt`` already rests on: the send that
        just succeeded delivered the *current* description, so whatever
        drop was recorded earlier — by this site or another — is
        discharged. Before the amendment, this suspenders-only recovery
        path did not exist: the drop had to wait for a belt call or a
        reconnect (``capture_session``), never a same-site retry.
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
        session_after_drop: object = _descriptions._session
        assert session_after_drop is None

        # A later suspenders send succeeds on its own terms — no request
        # context, no reconnect, just the next poller tick reaching a live
        # session again.
        recovered = MagicMock(spec=ServerSession)
        recovered.send_tool_list_changed = AsyncMock()
        _descriptions._session = recovered

        await _descriptions.notify_tool_list_changed()

        recovered.send_tool_list_changed.assert_awaited_once()
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
