"""Tests for TTY identity utilities."""

from __future__ import annotations

import pytest

from biff.relay import LocalRelay
from biff.tty import (
    build_session_key,
    claim_tty_name,
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
        assert next_tty_name(["custom", "tty2"]) == "tty1"

    def test_fills_lowest_gap(self) -> None:
        """next_tty_name fills the lowest gap, not max+1."""
        assert next_tty_name(["tty1", "tty3"]) == "tty2"

    def test_reuses_lowest_gap(self) -> None:
        assert next_tty_name(["tty1", "tty3", "tty5"]) == "tty2"

    def test_reuses_below_existing(self) -> None:
        assert next_tty_name(["tty2"]) == "tty1"


class TestClaimTtyName:
    """Atomic TTY name reservation via relay (DES-035)."""

    async def test_sequential_claims_distinct(self, tmp_path: object) -> None:
        """Sequential claims return distinct names."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        name1 = await claim_tty_name(relay, "kai", "kai:aaa1")
        name2 = await claim_tty_name(relay, "kai", "kai:bbb2")
        assert name1 == "tty1"
        assert name2 == "tty2"
        assert name1 != name2

    async def test_preferred_when_taken_raises(self, tmp_path: object) -> None:
        """claim_tty_name(preferred='deploy') when taken raises ValueError."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        name = await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        assert name == "deploy"
        with pytest.raises(ValueError, match="already in use"):
            await claim_tty_name(relay, "kai", "kai:bbb2", preferred="deploy")

    async def test_fills_gaps(self, tmp_path: object) -> None:
        """Reserve tty1, tty3 → next claim gets tty2."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        await claim_tty_name(relay, "kai", "kai:aaa1", preferred="tty1")
        await claim_tty_name(relay, "kai", "kai:bbb2", preferred="tty3")
        name = await claim_tty_name(relay, "kai", "kai:ccc3")
        assert name == "tty2"

    async def test_different_users_same_name(self, tmp_path: object) -> None:
        """Different users can claim the same name."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        name1 = await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        name2 = await claim_tty_name(relay, "eric", "eric:bbb2", preferred="deploy")
        assert name1 == "deploy"
        assert name2 == "deploy"

    async def test_release_and_reclaim(self, tmp_path: object) -> None:
        """After releasing, the same name can be reclaimed."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        await relay.release_tty_name("kai", "deploy")
        name = await claim_tty_name(relay, "kai", "kai:bbb2", preferred="deploy")
        assert name == "deploy"

    async def test_list_reserved_names(self, tmp_path: object) -> None:
        """list_reserved_names returns all reserved names for a user."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        await claim_tty_name(relay, "kai", "kai:aaa1")
        await claim_tty_name(relay, "kai", "kai:bbb2", preferred="deploy")
        names = await relay.list_reserved_names("kai")
        assert sorted(names) == ["deploy", "tty1"]
