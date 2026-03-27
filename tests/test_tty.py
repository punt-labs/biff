"""Tests for TTY identity utilities."""

from __future__ import annotations

import asyncio

import pytest

from biff.models import UserSession
from biff.relay import LocalRelay
from biff.tty import (
    assign_unique_tty_name,
    build_session_key,
    generate_tty,
    is_notification_for_session,
    next_tty_name,
    parse_address,
    validate_tty_name,
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


class TestValidateTtyName:
    """TTY name allowlist prevents terminal escape injection (biff-gvoj)."""

    def test_valid_alphanumeric(self) -> None:
        assert validate_tty_name("tty1") is None

    def test_valid_with_hyphens_underscores(self) -> None:
        assert validate_tty_name("my-tty_2") is None

    def test_rejects_ansi_escape(self) -> None:
        assert validate_tty_name("\033[31mred\033[0m") is not None

    def test_rejects_spaces(self) -> None:
        assert validate_tty_name("my tty") is not None

    def test_rejects_empty(self) -> None:
        assert validate_tty_name("") is not None

    def test_rejects_too_long(self) -> None:
        assert validate_tty_name("a" * 21) is not None

    def test_accepts_max_length(self) -> None:
        assert validate_tty_name("a" * 20) is None


class TestNextTtyName:
    def test_empty_starts_at_1(self) -> None:
        assert next_tty_name([]) == "tty1"

    def test_increments(self) -> None:
        assert next_tty_name(["tty1", "tty2"]) == "tty3"

    def test_ignores_non_sequential(self) -> None:
        assert next_tty_name(["custom", "tty2"]) == "tty3"

    def test_gaps_filled_above(self) -> None:
        """next_tty_name picks max+1, it does NOT fill gaps."""
        assert next_tty_name(["tty1", "tty3"]) == "tty4"


class _RacingRelay:
    """Relay wrapper that forces read-before-write interleaving.

    Both callers must complete their initial get_sessions() before
    either can proceed to update_session().  This simulates the
    worst-case NATS KV race where two sessions read the same state
    before either writes.
    """

    def __init__(self, relay: LocalRelay, num_racers: int = 2) -> None:
        self._relay = relay
        self._barrier = asyncio.Barrier(num_racers)
        self._read_count = 0

    async def get_sessions(self) -> list[UserSession]:
        result = await self._relay.get_sessions()
        self._read_count += 1
        # Synchronize after the first read (the "compute candidate" read).
        # Only barrier on the first read per caller, not the verify read.
        if self._read_count <= 2:
            await self._barrier.wait()
        return result

    async def get_session(self, session_key: str) -> UserSession | None:
        return await self._relay.get_session(session_key)

    async def update_session(self, session: UserSession) -> None:
        await self._relay.update_session(session)


class TestAssignUniqueTtyNameRace:
    """Concurrent TTY assignment must produce unique names."""

    async def test_two_sessions_get_different_names(self, tmp_path: object) -> None:
        from pathlib import Path

        data_dir = Path(str(tmp_path))
        relay = LocalRelay(data_dir)

        # Pre-register two sessions with empty tty_names.
        session_a = UserSession(user="kai", tty="aaaa1111")
        session_b = UserSession(user="kai", tty="bbbb2222")
        await relay.update_session(session_a)
        await relay.update_session(session_b)

        racing = _RacingRelay(relay)
        key_a = build_session_key("kai", "aaaa1111")
        key_b = build_session_key("kai", "bbbb2222")

        name_a, name_b = await asyncio.gather(
            assign_unique_tty_name(racing, key_a),  # type: ignore[arg-type]
            assign_unique_tty_name(racing, key_b),  # type: ignore[arg-type]
        )

        assert name_a != name_b, f"Both sessions got {name_a!r} — TTY race not resolved"
        assert {name_a, name_b} == {"tty1", "tty2"}
