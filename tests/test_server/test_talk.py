"""Unit tests for talk — message formatting, state management, constants."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from biff.models import BiffConfig, Message, UserSession
from biff.nats_relay import NatsRelay
from biff.server.state import create_state
from biff.server.tools.talk import (
    _NO_MESSAGES,
    _reset_talk,
    _resolve_talk_target,
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
        assert result == "[10:30:45] @kai: check PR #42"

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
        assert "@kai: first" in lines[0]
        assert "@eric: second" in lines[1]

    def test_empty_list(self) -> None:
        assert format_talk_messages([]) == ""


class TestTalkState:
    def test_initial_state_is_none(self) -> None:
        _reset_talk()
        from biff.server.tools._descriptions import get_talk_partner

        assert get_talk_partner() is None

    def test_reset_clears_partner(self) -> None:
        from biff.server.tools._descriptions import get_talk_partner, set_talk_partner

        set_talk_partner("eric")
        _reset_talk()
        assert get_talk_partner() is None


class TestResolveTalkTarget:
    """_resolve_talk_target resolves friendly tty_name to session key."""

    async def test_no_tty_returns_user(self) -> None:
        """Without tty, relay_key and display are both the username."""
        state = create_state(
            BiffConfig(user="kai", repo_name="_test"),
            Path("/tmp/test"),
            tty="abc123",
        )
        relay_key, display, target_repo = await _resolve_talk_target(
            state.relay, "eric", None
        )
        assert relay_key == "eric"
        assert display == "eric"
        assert target_repo is None

    async def test_literal_tty_resolves(self, tmp_path: Path) -> None:
        """When tty matches a literal session key, uses that key."""
        state = create_state(
            BiffConfig(user="kai", repo_name="_test"),
            tmp_path,
            tty="abc123",
        )
        # Create eric's session with a known tty hex ID
        await state.relay.update_session(UserSession(user="eric", tty="def456"))
        relay_key, display, target_repo = await _resolve_talk_target(
            state.relay, "eric", "def456"
        )
        assert relay_key == "eric:def456"
        assert display == "eric:def456"
        assert target_repo is None

    async def test_tty_name_resolves_to_hex(self, tmp_path: Path) -> None:
        """Friendly tty_name resolves to the session's actual hex key."""
        state = create_state(
            BiffConfig(user="kai", repo_name="_test"),
            tmp_path,
            tty="abc123",
        )
        # Create eric's session with a hex tty and a friendly name
        await state.relay.update_session(
            UserSession(user="eric", tty="def456", tty_name="laptop")
        )
        relay_key, display, target_repo = await _resolve_talk_target(
            state.relay, "eric", "laptop"
        )
        # relay_key should be the hex key, display keeps friendly name
        assert relay_key == "eric:def456"
        assert display == "eric:laptop"
        assert target_repo is None

    async def test_unresolved_tty_falls_back(self, tmp_path: Path) -> None:
        """Unknown tty falls back to raw value (best-effort delivery)."""
        state = create_state(
            BiffConfig(user="kai", repo_name="_test"),
            tmp_path,
            tty="abc123",
        )
        relay_key, display, target_repo = await _resolve_talk_target(
            state.relay, "eric", "unknown"
        )
        assert relay_key == "eric:unknown"
        assert display == "eric:unknown"
        assert target_repo is None


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
        assert "No new messages" in _NO_MESSAGES
        assert "listening" in _NO_MESSAGES.lower()
