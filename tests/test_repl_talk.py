"""Tests for REPL talk functions (biff.__main__ talk subsystem).

Coverage for the Z specification docs/talk.tex: handshake detection,
notification queue draining, accept checking, and message publishing.
All tests use mock queues — no NATS, no network.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from biff.__main__ import (
    _check_for_accept,
    _drain_talk_messages,
    _drain_talk_notifications,
    _has_pending_invite,
    _talk_publish,
)
from biff.cli_session import CliContext
from biff.models import BiffConfig


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
        pending: set[str] = set()
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
        assert "eric" in pending
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
        pending: set[str] = set()
        lines = _drain_talk_notifications(q, MY_KEY, pending)
        assert lines == []
        assert pending == set()

    def test_none_queue(self) -> None:
        lines = _drain_talk_notifications(None, MY_KEY)
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


# -----------------------------------------------------------------------
# Phase 2: _has_pending_invite
# -----------------------------------------------------------------------


class TestHasPendingInvite:
    def test_found(self) -> None:
        pending = {"eric", "priya"}
        assert _has_pending_invite(pending, "eric") is True

    def test_not_found(self) -> None:
        pending = {"priya"}
        assert _has_pending_invite(pending, "eric") is False
        assert pending == {"priya"}  # Unchanged

    def test_consumes_one_shot(self) -> None:
        pending = {"eric"}
        assert _has_pending_invite(pending, "eric") is True
        assert _has_pending_invite(pending, "eric") is False
        assert pending == set()

    def test_empty_set(self) -> None:
        pending: set[str] = set()
        assert _has_pending_invite(pending, "eric") is False


# -----------------------------------------------------------------------
# Phase 3: _check_for_accept
# -----------------------------------------------------------------------


class TestCheckForAccept:
    def test_found(self) -> None:
        q = _make_queue(
            [
                {"type": "accept", "from": "eric", "from_key": OTHER_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY) is True

    def test_skips_self_echo(self) -> None:
        q = _make_queue(
            [
                {"type": "accept", "from": "kai", "from_key": MY_KEY},
            ]
        )
        assert _check_for_accept(q, MY_KEY) is False

    def test_empty_queue(self) -> None:
        q = _make_queue([])
        assert _check_for_accept(q, MY_KEY) is False

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
        result = _check_for_accept(q, MY_KEY)
        assert result is False
        captured = capsys.readouterr()
        assert "priya" in captured.out
        assert "wants to talk" in captured.out

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
        assert _check_for_accept(q, MY_KEY) is True


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
        await _talk_publish(mock_nats_ctx, "eric", "message", "hello")
        subject, payload = await self._get_published(mock_nats_ctx)
        assert subject == "biff.test.talk.notify.eric"
        assert payload["type"] == "message"
        assert payload["from"] == "kai"
        assert payload["from_tty"] == "tty1"
        assert payload["body"] == "hello"
        assert payload["from_key"] == "kai:abc12345"

    @pytest.mark.anyio()
    async def test_publish_end(self, mock_nats_ctx: CliContext) -> None:
        await _talk_publish(mock_nats_ctx, "eric", "end")
        _, payload = await self._get_published(mock_nats_ctx)
        assert payload["type"] == "end"
        assert payload["body"] == ""

    @pytest.mark.anyio()
    async def test_publish_body_passthrough(self, mock_nats_ctx: CliContext) -> None:
        long_body = "x" * 1000
        await _talk_publish(mock_nats_ctx, "eric", "message", long_body)
        _, payload = await self._get_published(mock_nats_ctx)
        # _talk_publish sends the body as-is; truncation is the caller's job
        # (line[:512] in _repl_talk). Verify the body passes through.
        assert payload["body"] == long_body

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
        await _talk_publish(ctx, "eric", "message", "hello")
