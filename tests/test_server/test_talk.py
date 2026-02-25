"""Unit tests for talk — message formatting, state management, constants."""

from __future__ import annotations

from datetime import UTC, datetime

from biff.models import Message
from biff.server.tools.talk import (
    _NO_MESSAGES,
    _reset_talk,
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


class TestConstants:
    def test_no_messages_sentinel(self) -> None:
        assert "No new messages" in _NO_MESSAGES
        assert "listening" in _NO_MESSAGES.lower()
