"""Tests for REPL talk functions (biff.__main__ talk subsystem).

Coverage for the Z specification docs/talk.tex: handshake detection,
notification queue draining, accept checking, message publishing,
rejected partitions, and boundary conditions.  All tests use mock
queues — no NATS, no network.

Partition numbers reference the TTF partition table generated from
the Z spec via ``/z-spec:partition``.
"""

from __future__ import annotations

import asyncio
import json
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.__main__ import (
    _AcceptOutcome,
    _check_for_accept,
    _consume_pending_invite,
    _drain_talk_messages,
    _drain_talk_notifications,
    _talk_publish,
)
from biff.cli_session import CliContext
from biff.models import BiffConfig
from biff.repl_display import ReplDisplay


def _make_queue(
    items: list[dict[str, str]],
) -> asyncio.Queue[dict[str, str]]:
    """Build an asyncio.Queue pre-loaded with items."""
    q: asyncio.Queue[dict[str, str]] = asyncio.Queue()
    for item in items:
        q.put_nowait(item)
    return q


MY_KEY = "kai:abc12345"
OTHER_KEY = "eric:def67890"


# -----------------------------------------------------------------------
# Phase 1: _drain_talk_messages
# -----------------------------------------------------------------------


class TestDrainTalkMessages:
    def test_empty_queue(self) -> None:
        q = _make_queue([])
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert lines == []
        assert ended is False

    def test_none_queue(self) -> None:
        lines, ended = _drain_talk_messages(None, MY_KEY)
        assert lines == []
        assert ended is False

    def test_filters_invite_and_accept(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "eric",
                    "body": "wants to talk",
                    "from_key": OTHER_KEY,
                },
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert lines == []
        assert ended is False

    def test_self_echo_suppressed(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "kai",
                    "from_tty": "tty1",
                    "body": "hello",
                    "from_key": MY_KEY,
                },
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert lines == []
        assert ended is False

    def test_end_sets_ended(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "end",
                    "from": "eric",
                    "from_tty": "tty2",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is True
        assert len(lines) == 1
        assert "ended the conversation" in lines[0]
        assert "eric:tty2" in lines[0]

    def _message_queue(self) -> asyncio.Queue[dict[str, str]]:
        return _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "hello",
                    "from_key": OTHER_KEY,
                },
            ]
        )

    def test_no_timestamp_without_display(self) -> None:
        """Default (no display) keeps the historical timestamp-free line."""
        lines, _ = _drain_talk_messages(self._message_queue(), MY_KEY)
        assert len(lines) == 1
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None
        assert "eric:tty2 ▶ hello" in lines[0]

    def test_no_timestamp_when_display_off(self) -> None:
        """A display with timestamps off renders no stamp."""
        display = ReplDisplay()
        lines, _ = _drain_talk_messages(self._message_queue(), MY_KEY, display)
        assert re.search(r"\[\d{2}:\d{2}\]", lines[0]) is None

    def test_timestamp_prefix_when_display_on(self) -> None:
        """A display with timestamps on prefixes the message with [HH:MM]."""
        display = ReplDisplay()
        display.set_timestamps(on=True)
        lines, _ = _drain_talk_messages(self._message_queue(), MY_KEY, display)
        assert len(lines) == 1
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hello", lines[0]) is not None

    def test_escape_injection_in_body_is_neutralized(self) -> None:
        """A remote body cannot inject terminal escapes into our output."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "clear\x1b[2Jme\x1b]0;pwned\x07",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, _ = _drain_talk_messages(q, MY_KEY)
        # The dangerous escapes are gone; only our own color codes remain.
        assert "\x1b[2J" not in lines[0]
        assert "\x1b]0;" not in lines[0]
        assert "\x07" not in lines[0]
        # The printable remainder survives, defanged.
        assert "clear[2Jme]0;pwned" in lines[0]

    def test_escape_injection_in_sender_is_neutralized(self) -> None:
        """A remote sender/tty cannot inject terminal escapes either."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "e\x1b[2Jvil",
                    "from_tty": "tty2",
                    "body": "hi",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, _ = _drain_talk_messages(q, MY_KEY)
        assert "\x1b[2J" not in lines[0]
        assert "e[2Jvil:tty2 ▶ hi" in lines[0]

    def test_message_conversation_style(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "hello there",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is False
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hello there" in lines[0]
        # Cyan color, no phone emoji
        assert "\033[36m" in lines[0]
        assert "📞" not in lines[0]

    def test_message_without_tty(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "body": "hi",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, _ = _drain_talk_messages(q, MY_KEY)
        assert len(lines) == 1
        # No colon separator when from_tty is missing
        assert "eric ▶ hi" in lines[0]

    def test_multiple_messages_all_formatted(self) -> None:
        """Partition 26: two messages drained, both formatted."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "first",
                    "from_key": OTHER_KEY,
                },
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "second",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is False
        assert len(lines) == 2
        assert "first" in lines[0]
        assert "second" in lines[1]

    def test_all_four_types_only_message_and_end_processed(self) -> None:
        """Partition 4/12: queue with all 4 notification types."""
        q = _make_queue(
            [
                {"type": "invite", "from": "a", "body": "inv", "from_key": "a:1"},
                {"type": "accept", "from": "b", "from_key": "b:2"},
                {
                    "type": "message",
                    "from": "c",
                    "from_tty": "tty3",
                    "body": "msg",
                    "from_key": "c:3",
                },
                {"type": "end", "from": "d", "from_tty": "tty4", "from_key": "d:4"},
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is True
        assert len(lines) == 2  # message + end, invite/accept skipped
        assert "msg" in lines[0]
        assert "ended the conversation" in lines[1]

    def test_other_key_not_suppressed(self) -> None:
        """Partition 20/8: other key messages are NOT suppressed."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "visible",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, _ = _drain_talk_messages(q, MY_KEY)
        assert len(lines) == 1
        assert "visible" in lines[0]

    def test_end_does_not_set_ended_for_message(self) -> None:
        """Partition 35: ntMessage does NOT set ended."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "still here",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        _, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is False

    def test_end_without_tty(self) -> None:
        """Partition 30: end notification without from_tty."""
        q = _make_queue(
            [
                {"type": "end", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is True
        assert "eric" in lines[0]
        # No colon when tty missing
        assert "eric has ended" in lines[0]

    def test_empty_body_message_not_formatted(self) -> None:
        """Partition 47 boundary: message with empty body produces no line."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, _ = _drain_talk_messages(q, MY_KEY)
        assert lines == []

    def test_mixed_messages_and_end(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "bye",
                    "from_key": OTHER_KEY,
                },
                {
                    "type": "end",
                    "from": "eric",
                    "from_tty": "tty2",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines, ended = _drain_talk_messages(q, MY_KEY)
        assert ended is True
        assert len(lines) == 2


# -----------------------------------------------------------------------
# Phase 1: _drain_talk_notifications (REPL mode, not talk mode)
# -----------------------------------------------------------------------


class TestDrainTalkNotifications:
    def test_records_pending_invites(self) -> None:
        pending: dict[str, str] = {}
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "eric",
                    "body": "wants to talk",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY, pending)
        # The inviter's session key is recorded so the accept can target it.
        assert pending["eric"] == OTHER_KEY
        assert len(lines) == 1
        assert "📞" in lines[0]

    def test_skips_accept(self) -> None:
        q = _make_queue(
            [
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY)
        assert lines == []

    def test_self_echo_suppressed(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "kai",
                    "body": "wants to talk",
                    "from_key": MY_KEY,
                },
            ]
        )
        pending: dict[str, str] = {}
        lines = _drain_talk_notifications(q, MY_KEY, pending)
        assert lines == []
        assert pending == {}

    def test_none_queue(self) -> None:
        lines = _drain_talk_notifications(None, MY_KEY)
        assert lines == []

    def test_newer_invite_supersedes(self) -> None:
        """A newer invite from the same user overrides the older session key."""
        pending: dict[str, str] = {"eric": "eric:oldsess"}
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "eric",
                    "body": "again",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        _drain_talk_notifications(q, MY_KEY, pending)
        # Latest invite wins — supersede (DES-043).
        assert pending == {"eric": OTHER_KEY}

    def test_multiple_invite_senders(self) -> None:
        """Partition 14: two different invite senders both recorded."""
        pending: dict[str, str] = {}
        q = _make_queue(
            [
                {"type": "invite", "from": "eric", "body": "hi", "from_key": OTHER_KEY},
                {
                    "type": "invite",
                    "from": "priya",
                    "body": "hey",
                    "from_key": "priya:xyz",
                },
            ]
        )
        _drain_talk_notifications(q, MY_KEY, pending)
        assert pending == {"eric": OTHER_KEY, "priya": "priya:xyz"}

    def test_no_pending_invites_without_set(self) -> None:
        """Invites are NOT recorded when pending_invites is None."""
        q = _make_queue(
            [
                {"type": "invite", "from": "eric", "body": "hi", "from_key": OTHER_KEY},
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY, None)
        assert len(lines) == 1  # Still displayed
        # But no set to record into

    def test_end_notification_in_repl_mode(self) -> None:
        """End notifications outside talk mode display with label."""
        q = _make_queue(
            [
                {
                    "type": "end",
                    "from": "eric",
                    "from_tty": "tty2",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY)
        # end type has no body, so no line produced
        assert lines == []

    def test_message_in_repl_mode(self) -> None:
        """Regular messages outside talk mode show with sender prefix."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "hi there",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY)
        assert len(lines) == 1
        assert "eric:tty2" in lines[0]
        assert "hi there" in lines[0]

    def test_banner_stamped_when_display_on(self) -> None:
        """The idle-prompt message banner honors the timestamps toggle too."""
        display = ReplDisplay()
        display.set_timestamps(on=True)
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "hi there",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY, None, display)
        assert len(lines) == 1
        assert re.search(r"\[\d{2}:\d{2}\] eric:tty2 ▶ hi there", lines[0]) is not None

    def test_banner_escape_injection_is_neutralized(self) -> None:
        """The idle-prompt banner also strips remote terminal escapes."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "from_tty": "tty2",
                    "body": "hi\x1b[2Jthere",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        lines = _drain_talk_notifications(q, MY_KEY)
        assert "\x1b[2J" not in lines[0]
        assert "hi[2Jthere" in lines[0]


# -----------------------------------------------------------------------
# Phase 2: _has_pending_invite
# -----------------------------------------------------------------------


class TestConsumePendingInvite:
    def test_found_returns_key(self) -> None:
        pending = {"eric": OTHER_KEY, "priya": "priya:xyz"}
        assert _consume_pending_invite(pending, "eric") == OTHER_KEY

    def test_not_found_returns_none(self) -> None:
        pending = {"priya": "priya:xyz"}
        assert _consume_pending_invite(pending, "eric") is None
        assert pending == {"priya": "priya:xyz"}  # Unchanged

    def test_consumes_one_shot(self) -> None:
        pending = {"eric": OTHER_KEY}
        assert _consume_pending_invite(pending, "eric") == OTHER_KEY
        assert _consume_pending_invite(pending, "eric") is None
        assert pending == {}

    def test_empty_map(self) -> None:
        """Partition 37/31: empty map → initiator path."""
        pending: dict[str, str] = {}
        assert _consume_pending_invite(pending, "eric") is None

    def test_consume_one_of_many(self) -> None:
        """Partition 43/15: consume eric, priya remains."""
        pending = {"eric": OTHER_KEY, "priya": "priya:xyz"}
        assert _consume_pending_invite(pending, "eric") == OTHER_KEY
        assert pending == {"priya": "priya:xyz"}

    def test_empty_key_treated_as_none(self) -> None:
        """An invite with no session key can't be targeted — treat as absent."""
        pending = {"eric": ""}
        assert _consume_pending_invite(pending, "eric") is None


# -----------------------------------------------------------------------
# Phase 3: _check_for_accept
# -----------------------------------------------------------------------


class TestCheckForAccept:
    # We (MY_KEY) invited OTHER_KEY; a normal accept comes from OTHER_KEY.
    _TARGET = OTHER_KEY
    _THIRD_PARTY = "zed:999999"  # not the session we invited

    def test_found(self) -> None:
        q = _make_queue(
            [
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.ACCEPTED

    def test_skips_self_echo(self) -> None:
        q = _make_queue(
            [
                {"type": "accept", "from": "kai", "from_key": MY_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_empty_queue(self) -> None:
        q = _make_queue([])
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_displays_non_accept_banners(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "priya",
                    "body": "wants to talk",
                    "from_key": "priya:xyz",
                },
            ]
        )
        result = _check_for_accept(q, MY_KEY, self._TARGET)
        assert result is _AcceptOutcome.NONE
        captured = capsys.readouterr()
        assert "priya" in captured.out
        assert "wants to talk" in captured.out

    def test_message_not_treated_as_accept(self) -> None:
        """Partition 24: ntMessage at head → not accept."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "eric",
                    "body": "hi",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_end_not_treated_as_accept(self) -> None:
        """Partition 24: ntEnd at head → not accept."""
        q = _make_queue(
            [
                {"type": "end", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_third_party_invite_not_treated_as_accept(self) -> None:
        """An invite from a session we did not invite is only a banner."""
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "zed",
                    "body": "talk?",
                    "from_key": self._THIRD_PARTY,
                },
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_third_party_accept_not_treated_as_accept(self) -> None:
        """An accept from a session we did not invite must be ignored.

        Consent boundary: a third party who publishes a targeted accept
        (to_key == our session) must not make us believe the invited peer
        accepted.  Only an accept whose from_key is the invited target counts.
        """
        q = _make_queue(
            [
                {"type": "accept", "from": "zed", "from_key": self._THIRD_PARTY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.NONE

    def test_mutual_invite_higher_key_auto_accepts(self) -> None:
        """We (higher key) invited them; their invite back → AUTO_ACCEPT."""
        # MY_KEY 'kai:...' > OTHER_KEY 'eric:...' lexicographically.
        assert MY_KEY > OTHER_KEY
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "eric",
                    "body": "talk?",
                    "from_key": OTHER_KEY,
                },
            ]
        )
        outcome = _check_for_accept(q, MY_KEY, OTHER_KEY)
        assert outcome is _AcceptOutcome.AUTO_ACCEPT

    def test_mutual_invite_lower_key_keeps_waiting(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """We (lower key) invited them; their invite back → stay inviting."""
        # From eric's side: OTHER_KEY < MY_KEY, so eric stays the inviter.
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "kai",
                    "body": "talk?",
                    "from_key": MY_KEY,
                },
            ]
        )
        outcome = _check_for_accept(q, OTHER_KEY, MY_KEY)
        assert outcome is _AcceptOutcome.NONE
        # No banner spam for the partner's mutual invite.
        assert "talk?" not in capsys.readouterr().out

    def test_accept_beats_mutual_invite(self) -> None:
        """A real accept present alongside a mutual invite wins as ACCEPTED."""
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "eric",
                    "body": "talk?",
                    "from_key": OTHER_KEY,
                },
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, OTHER_KEY) is _AcceptOutcome.ACCEPTED

    def test_accept_drains_entire_queue(self) -> None:
        """Partition 8/20: accept with trailing items, all drained."""
        q = _make_queue(
            [
                {
                    "type": "message",
                    "from": "priya",
                    "body": "hey",
                    "from_key": "priya:xyz",
                },
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
                {
                    "type": "invite",
                    "from": "bob",
                    "body": "talk?",
                    "from_key": "bob:123",
                },
            ]
        )
        result = _check_for_accept(q, MY_KEY, self._TARGET)
        assert result is _AcceptOutcome.ACCEPTED
        assert q.empty()  # All items drained

    def test_accept_among_other_notifications(self) -> None:
        q = _make_queue(
            [
                {
                    "type": "invite",
                    "from": "priya",
                    "body": "hey",
                    "from_key": "priya:xyz",
                },
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY, self._TARGET) is _AcceptOutcome.ACCEPTED


# -----------------------------------------------------------------------
# Phase 4: _talk_publish
# -----------------------------------------------------------------------


class TestTalkPublish:
    @pytest.fixture()
    def mock_nats_ctx(self) -> CliContext:
        """CliContext with a mock NatsRelay."""
        from biff.nats_relay import NatsRelay

        relay = MagicMock(spec=NatsRelay)
        nc = AsyncMock()
        relay.get_nc = AsyncMock(return_value=nc)
        relay.talk_notify_subject = MagicMock(return_value="biff.test.talk.notify.eric")
        return CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
            tty_name="tty1",
        )

    async def _get_published(  # pyright: ignore[reportUnknownParameterType]
        self, ctx: CliContext
    ) -> tuple[str, dict[str, str]]:
        """Extract published subject and payload from the mock nc."""
        nc = await ctx.relay.get_nc()  # type: ignore[attr-defined]  # pyright: ignore[reportUnknownMemberType]
        nc.publish.assert_awaited_once()  # pyright: ignore[reportUnknownMemberType]
        pos = nc.publish.call_args[0]  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        subject: str = str(pos[0])  # pyright: ignore[reportUnknownArgumentType]
        payload: dict[str, str] = json.loads(pos[1])  # pyright: ignore[reportUnknownArgumentType]
        return subject, payload

    @pytest.mark.anyio()
    async def test_publish_message(self, mock_nats_ctx: CliContext) -> None:
        await _talk_publish(
            mock_nats_ctx, "eric", "message", "hello", to_key="eric:def67890"
        )
        subject, payload = await self._get_published(mock_nats_ctx)
        assert subject == "biff.test.talk.notify.eric"
        assert payload["type"] == "message"
        assert payload["from"] == "kai"
        assert payload["from_tty"] == "tty1"
        assert payload["body"] == "hello"
        assert payload["from_key"] == "kai:abc12345"
        # Session-scoped: to_key routes the notification to one session.
        assert payload["to_key"] == "eric:def67890"

    @pytest.mark.anyio()
    async def test_publish_end(self, mock_nats_ctx: CliContext) -> None:
        await _talk_publish(mock_nats_ctx, "eric", "end", to_key="eric:def67890")
        _, payload = await self._get_published(mock_nats_ctx)
        assert payload["type"] == "end"
        assert payload["body"] == ""
        assert payload["to_key"] == "eric:def67890"

    @pytest.mark.anyio()
    async def test_publish_body_passthrough(self, mock_nats_ctx: CliContext) -> None:
        long_body = "x" * 1000
        await _talk_publish(
            mock_nats_ctx, "eric", "message", long_body, to_key="eric:def67890"
        )
        _, payload = await self._get_published(mock_nats_ctx)
        # _talk_publish sends the body as-is; truncation is the caller's job
        # (line[:512] in _repl_talk). Verify the body passes through.
        assert payload["body"] == long_body

    @pytest.mark.anyio()
    async def test_publish_empty_body(self, mock_nats_ctx: CliContext) -> None:
        """Partition 47: empty body is valid."""
        await _talk_publish(
            mock_nats_ctx, "eric", "message", "", to_key="eric:def67890"
        )
        _, payload = await self._get_published(mock_nats_ctx)
        assert payload["body"] == ""

    @pytest.mark.anyio()
    async def test_publish_invite(self, mock_nats_ctx: CliContext) -> None:
        """Partition: invite type published correctly."""
        await _talk_publish(
            mock_nats_ctx, "eric", "invite", "wants to talk", to_key="eric:def67890"
        )
        _, payload = await self._get_published(mock_nats_ctx)
        assert payload["type"] == "invite"
        assert payload["body"] == "wants to talk"
        assert payload["to_key"] == "eric:def67890"

    @pytest.mark.anyio()
    async def test_publish_accept(self, mock_nats_ctx: CliContext) -> None:
        """Partition: accept type published correctly."""
        await _talk_publish(mock_nats_ctx, "eric", "accept", to_key="eric:def67890")
        _, payload = await self._get_published(mock_nats_ctx)
        assert payload["type"] == "accept"
        assert payload["to_key"] == "eric:def67890"

    @pytest.mark.anyio()
    async def test_non_nats_relay_noop(self) -> None:
        from biff.relay import LocalRelay

        relay = MagicMock(spec=LocalRelay)
        ctx = CliContext(
            relay=relay,
            config=BiffConfig(user="kai", repo_name="test"),
            session_key="kai:abc12345",
            user="kai",
            tty="abc12345",
            tty_name="tty1",
        )
        # Should not raise, should do nothing.
        await _talk_publish(ctx, "eric", "message", "hello", to_key="eric:def67890")
