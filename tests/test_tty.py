"""Tests for TTY identity utilities."""

from __future__ import annotations

import pytest

from biff.tty import (
    build_session_key,
    generate_tty,
    is_notification_for_session,
    parse_address,
)


class TestGenerateTTY:
    def test_length(self) -> None:
        assert len(generate_tty()) == 8

    def test_hex_chars(self) -> None:
        tty = generate_tty()
        int(tty, 16)  # Raises ValueError if not valid hex

    def test_unique(self) -> None:
        assert generate_tty() != generate_tty()


class TestBuildSessionKey:
    def test_format(self) -> None:
        assert build_session_key("kai", "a1b2c3d4") == "kai:a1b2c3d4"


class TestParseAddress:
    def test_bare_user(self) -> None:
        assert parse_address("kai") == ("kai", None)

    def test_at_prefix(self) -> None:
        assert parse_address("@kai") == ("kai", None)

    def test_targeted(self) -> None:
        assert parse_address("kai:tty1") == ("kai", "tty1")

    def test_at_targeted(self) -> None:
        assert parse_address("@kai:tty1") == ("kai", "tty1")

    def test_whitespace_stripped(self) -> None:
        assert parse_address("  @kai : tty1  ") == ("kai", "tty1")

    def test_empty_tty_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty TTY"):
            parse_address("kai:")

    def test_at_empty_tty_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty TTY"):
            parse_address("@kai:")

    def test_whitespace_only_tty_raises(self) -> None:
        with pytest.raises(ValueError, match="Empty TTY"):
            parse_address("kai:   ")

    def test_multiple_colons_keeps_rest_in_tty(self) -> None:
        """Colons after the first are part of the TTY — rejected by relay validation."""
        assert parse_address("kai:tty1:extra") == ("kai", "tty1:extra")


class TestIsNotificationForSession:
    """to_key filtering for targeted vs broadcast notifications (biff-gvoj)."""

    def test_broadcast_accepted(self) -> None:
        """No to_key means broadcast — accepted by all sessions."""
        assert is_notification_for_session({"from": "kai"}, "eric:tty1")

    def test_matching_to_key_accepted(self) -> None:
        data = {"from": "kai", "to_key": "eric:tty1"}
        assert is_notification_for_session(data, "eric:tty1")

    def test_non_matching_to_key_rejected(self) -> None:
        data = {"from": "kai", "to_key": "eric:tty2"}
        assert not is_notification_for_session(data, "eric:tty1")

    def test_empty_to_key_treated_as_broadcast(self) -> None:
        data = {"from": "kai", "to_key": ""}
        assert is_notification_for_session(data, "eric:tty1")
