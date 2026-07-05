"""Unit tests for talk — message formatting, state management, constants."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from biff.models import Message, UserSession
from biff.nats_relay import NatsRelay
from biff.server.tools._session import resolve_talk_target
from biff.server.tools.talk import (
    _NO_MESSAGES,
    format_talk_messages,
)


class TestFormatTalkMessages:
    def test_single_message(self) -> None:
        msg = Message(
            from_user="kai",
            to_user="eric",
            body="check PR #42",
            timestamp=datetime(2026, 1, 15, 10, 30, 45, tzinfo=UTC),
        )
        result = format_talk_messages([msg])
        assert result == "[10:30:45] kai: check PR #42"

    def test_multiple_messages(self) -> None:
        msgs = [
            Message(
                from_user="kai",
                to_user="eric",
                body="first",
                timestamp=datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC),
            ),
            Message(
                from_user="eric",
                to_user="kai",
                body="second",
                timestamp=datetime(2026, 1, 15, 10, 0, 5, tzinfo=UTC),
            ),
        ]
        result = format_talk_messages(msgs)
        lines = result.split("\n")
        assert len(lines) == 2
        assert "kai: first" in lines[0]
        assert "eric: second" in lines[1]

    def test_empty_list(self) -> None:
        assert format_talk_messages([]) == ""

    def test_escapes_neutralized(self) -> None:
        """Remote body/sender can't inject terminal escapes (biff-lbj)."""
        msg = Message(
            from_user="ev\x1b[2Kil",
            to_user="kai",
            body="hi\x1b[2Jthere",
            timestamp=datetime(2026, 1, 15, 10, 30, 45, tzinfo=UTC),
        )
        result = format_talk_messages([msg])
        assert "\x1b[2J" not in result
        assert "\x1b[2K" not in result
        assert "hi[2Jthere" in result


class TestResolveTalkTarget:
    """resolve_talk_target maps a session address to a specific session key.

    Talk is session-scoped (DES-043): the address MUST name a session.
    """

    _SENDER = "kai:sender01"

    def test_bare_user_errors(self) -> None:
        """A bare @user has no unambiguous session — reject with a hint."""
        sessions = [UserSession(user="eric", tty="def456")]
        with pytest.raises(ValueError, match="specific session"):
            resolve_talk_target(sessions, "eric", None, sender_key=self._SENDER)

    def test_literal_tty_resolves(self) -> None:
        """When tty matches a literal session key, uses that key."""
        sessions = [UserSession(user="eric", tty="def456")]
        relay_key, display, target_repo = resolve_talk_target(
            sessions, "eric", "def456", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:def456"
        assert target_repo is None

    def test_tty_name_resolves_to_hex(self) -> None:
        """Friendly tty_name resolves to the session's actual hex key."""
        sessions = [UserSession(user="eric", tty="def456", tty_name="laptop")]
        relay_key, display, target_repo = resolve_talk_target(
            sessions, "eric", "laptop", sender_key=self._SENDER
        )
        assert relay_key == "eric:def456"
        assert display == "eric:laptop"
        assert target_repo is None

    def test_unresolved_tty_falls_back(self) -> None:
        """Unknown tty falls back to raw value (best-effort delivery)."""
        relay_key, display, target_repo = resolve_talk_target(
            [], "eric", "unknown", sender_key=self._SENDER
        )
        assert relay_key == "eric:unknown"
        assert display == "eric:unknown"
        assert target_repo is None

    def test_reaches_only_named_session(self) -> None:
        """Two sessions for one user — only the named tty is targeted."""
        sessions = [
            UserSession(user="eric", tty="aaa111", tty_name="laptop"),
            UserSession(user="eric", tty="bbb222", tty_name="desktop"),
        ]
        relay_key, _, _ = resolve_talk_target(
            sessions, "eric", "desktop", sender_key=self._SENDER
        )
        assert relay_key == "eric:bbb222"

    def test_self_talk_rejected(self) -> None:
        """Resolving to the sender's own session key is refused."""
        sessions = [UserSession(user="kai", tty="sender01", tty_name="here")]
        with pytest.raises(ValueError, match="your own session"):
            resolve_talk_target(sessions, "kai", "here", sender_key=self._SENDER)

    def test_cross_repo_sets_target_repo(self) -> None:
        """A session in a different repo yields its repo as target_repo."""
        sessions = [
            UserSession(user="eric", tty="ccc333", tty_name="peer", repo="other")
        ]
        relay_key, _, target_repo = resolve_talk_target(
            sessions, "eric", "peer", sender_key=self._SENDER, sender_repo="mine"
        )
        assert relay_key == "eric:ccc333"
        assert target_repo == "other"


class TestTalkNotificationToKey:
    """deliver's talk notification carries to_key for session-scoped targets."""

    def _relay_with_mock_nc(self) -> tuple[NatsRelay, AsyncMock]:
        relay = NatsRelay(
            url="nats://localhost", repo_name="myrepo", stream_prefix="biff-test"
        )
        nc = AsyncMock()
        nc.is_closed = False
        relay._nc = nc
        return relay, nc

    @staticmethod
    def _payload(nc: AsyncMock) -> dict[str, str]:
        nc.publish.assert_awaited_once()
        payload: dict[str, str] = json.loads(nc.publish.call_args[0][1])
        return payload

    async def test_targeted_sets_to_key(self) -> None:
        """A ``user:tty`` target puts to_key in the notification payload."""
        relay, nc = self._relay_with_mock_nc()
        msg = Message(
            from_user="kai", from_tty="tty1", to_user="eric:def456", body="hi"
        )
        await relay._publish_talk_notification("eric:def456", msg, "kai:abc123")
        assert self._payload(nc)["to_key"] == "eric:def456"

    async def test_broadcast_omits_to_key(self) -> None:
        """A bare ``user`` target (write/wall) has no to_key — broadcast."""
        relay, nc = self._relay_with_mock_nc()
        msg = Message(from_user="kai", from_tty="tty1", to_user="eric", body="hi")
        await relay._publish_talk_notification("eric", msg, "kai:abc123")
        assert "to_key" not in self._payload(nc)


class TestValidatedSenderKey:
    """NatsRelay._validated_sender_key drops invalid or mismatched keys."""

    def test_valid_key(self) -> None:
        result = NatsRelay._validated_sender_key("kai:abc123", "kai")
        assert result == "kai:abc123"

    def test_empty_key(self) -> None:
        assert NatsRelay._validated_sender_key("", "kai") == ""

    def test_no_colon(self) -> None:
        assert NatsRelay._validated_sender_key("kai", "kai") == ""

    def test_user_mismatch(self) -> None:
        assert NatsRelay._validated_sender_key("eric:abc123", "kai") == ""

    def test_empty_tty_part(self) -> None:
        assert NatsRelay._validated_sender_key("kai:", "kai") == ""

    def test_empty_user_part(self) -> None:
        assert NatsRelay._validated_sender_key(":abc123", "kai") == ""


class TestConstants:
    def test_no_messages_sentinel(self) -> None:
        assert "talk" in _NO_MESSAGES.lower()
