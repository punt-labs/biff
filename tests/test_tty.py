"""Tests for TTY identity utilities."""

from __future__ import annotations

import pytest

from biff.tty import build_session_key, generate_tty, parse_address


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
