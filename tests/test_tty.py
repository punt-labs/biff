"""Tests for TTY identity utilities."""

from __future__ import annotations

import pytest

from biff.relay import LocalRelay
from biff.tty import (
    build_session_key,
    claim_tty_name,
    format_address,
    generate_tty,
    is_notification_for_session,
    next_tty_name,
    parse_address,
    rename_tty,
    validate_reclaimable_name,
    validate_routing_id,
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

    def test_strips_exactly_one_at(self) -> None:
        """A single leading ``@`` is tolerated; a second is kept literally.

        ``lstrip('@')`` stripped *all* leading ``@`` — ``@@kai`` became a
        valid ``kai``.  ``removeprefix`` strips exactly one, so a malformed
        ``@@kai`` stays malformed (``@kai``) instead of being silently
        normalized into a valid address.
        """
        assert parse_address("@@kai") == ("@kai", None)

    def test_strips_one_at_on_targeted(self) -> None:
        assert parse_address("@@kai:tty1") == ("@kai", "tty1")

    def test_bare_is_canonical(self) -> None:
        """Bare ``user:tty`` (no sigil) is the canonical input form."""
        assert parse_address("kai:tty1") == ("kai", "tty1")


class TestFormatAddress:
    """Canonical address rendering is bare — no ``@`` sigil."""

    def test_user_and_tty(self) -> None:
        assert format_address("kai", "tty1") == "kai:tty1"

    def test_bare_user(self) -> None:
        assert format_address("kai") == "kai"

    def test_none_tty_is_bare_user(self) -> None:
        assert format_address("kai", None) == "kai"

    def test_no_at_sigil(self) -> None:
        assert not format_address("kai", "tty1").startswith("@")

    def test_round_trips_through_parse(self) -> None:
        """Formatting then parsing recovers the original address."""
        assert parse_address(format_address("kai", "tty1")) == ("kai", "tty1")
        assert parse_address(format_address("kai")) == ("kai", None)


class TestValidateRoutingId:
    """Routing-token validator admits the session_id (UUID) shape."""

    def test_accepts_uuid(self) -> None:
        assert validate_routing_id("2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b") is None

    def test_accepts_hex_fallback(self) -> None:
        assert validate_routing_id("a1b2c3d4") is None

    def test_accepts_derived_hex(self) -> None:
        assert validate_routing_id("0123456789abcdef") is None

    def test_rejects_dots(self) -> None:
        """Dots would split a single NATS subject token."""
        assert validate_routing_id("a.b") is not None

    def test_rejects_non_hex_letters(self) -> None:
        assert validate_routing_id("tty1") is not None

    def test_rejects_empty(self) -> None:
        assert validate_routing_id("") is not None

    def test_rejects_over_64(self) -> None:
        assert validate_routing_id("a" * 65) is not None

    def test_rejects_escape(self) -> None:
        assert validate_routing_id("\033[31m") is not None

    def test_rejects_all_hyphens(self) -> None:
        """An id needs at least one hex digit — '----' is not a routing id."""
        assert validate_routing_id("----") is not None

    def test_rejects_single_hyphen(self) -> None:
        assert validate_routing_id("-") is not None


class TestValidateReclaimableName:
    """The team-writable reclaim hint value must pass a tight guard."""

    def test_accepts_ttyn(self) -> None:
        assert validate_reclaimable_name("tty16") is None

    def test_accepts_human_alias(self) -> None:
        assert validate_reclaimable_name("deploy") is None

    def test_rejects_dotted(self) -> None:
        """A dotted value could inject a NATS subject / KV key segment."""
        assert validate_reclaimable_name("evil.name") is not None

    def test_rejects_sid_namespace_exact(self) -> None:
        assert validate_reclaimable_name("sid") is not None

    def test_rejects_sid_namespace_prefix(self) -> None:
        assert validate_reclaimable_name("sid.deadbeef") is not None

    def test_rejects_wildcard(self) -> None:
        assert validate_reclaimable_name("tty*") is not None

    def test_rejects_escape(self) -> None:
        assert validate_reclaimable_name("\033[31m") is not None

    def test_rejects_too_long(self) -> None:
        assert validate_reclaimable_name("a" * 21) is not None

    def test_rejects_empty(self) -> None:
        assert validate_reclaimable_name("") is not None


class TestIsNotificationForSession:
    """to_key filtering for targeted vs broadcast notifications."""

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
    """TTY name allowlist prevents terminal escape injection."""

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

    async def test_preferred_when_taken_by_other_raises(self, tmp_path: object) -> None:
        """A preferred name held by a DIFFERENT session raises ValueError."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        name = await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        assert name == "deploy"
        with pytest.raises(ValueError, match="already in use"):
            await claim_tty_name(relay, "kai", "kai:bbb2", preferred="deploy")

    async def test_same_identity_takeover_reclaims_alias(
        self, tmp_path: object
    ) -> None:
        """Our own prior incarnation's held alias is reclaimed, not rejected.

        On resume the session key is identical ({user}:{session_id} is stable),
        so a reservation still held by our just-exited session is ours to take
        back — the exit->resume overlap must not force a fresh alias.
        """
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        session_key = "kai:2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        # Prior incarnation holds tty16; it has not released yet.
        assert await relay.reserve_tty_name("kai", "tty16", session_key)
        # Resume as the SAME session_key reclaims the SAME alias.
        name = await claim_tty_name(relay, "kai", session_key, preferred="tty16")
        assert name == "tty16"
        assert await relay.get_tty_reservation_owner("kai", "tty16") == session_key

    async def test_takeover_reacquires_when_reservation_vanishes(
        self, tmp_path: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TOCTOU: if the reservation vanishes mid-takeover, re-acquire it.

        The owner check sees our key, but the reservation is released before
        the refresh (a no-op then).  claim must re-reserve atomically so it
        never returns an alias it does not actually hold (DES-035).
        """
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        session_key = "kai:2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        assert await relay.reserve_tty_name("kai", "tty16", session_key)

        async def _vanish(user: str, name: str, _sk: str) -> None:
            # Simulate the reservation being released in the TOCTOU window.
            await relay.release_tty_name(user, name)

        monkeypatch.setattr(relay, "refresh_tty_reservation", _vanish)

        name = await claim_tty_name(relay, "kai", session_key, preferred="tty16")
        assert name == "tty16"
        # The alias is actually held — not returned unheld.
        assert await relay.get_tty_reservation_owner("kai", "tty16") == session_key

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


class TestRenameTty:
    """Rename-to-same-name re-reserves after TTL lapse (DES-035)."""

    async def test_same_name_re_reserves_after_lapse(self, tmp_path: object) -> None:
        """rename_tty(preferred=old_name) re-reserves when reservation lapsed."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        # Simulate TTL expiry by releasing the reservation.
        await relay.release_tty_name("kai", "deploy")
        # rename_tty with same name should re-reserve successfully.
        name = await rename_tty(relay, "kai", "kai:aaa1", "deploy", preferred="deploy")
        assert name == "deploy"
        # Verify reservation exists.
        names = await relay.list_reserved_names("kai")
        assert "deploy" in names

    async def test_same_name_raises_when_stolen(self, tmp_path: object) -> None:
        """rename_tty raises ValueError when another session holds name."""
        from pathlib import Path

        relay = LocalRelay(Path(str(tmp_path)))
        await claim_tty_name(relay, "kai", "kai:aaa1", preferred="deploy")
        # Simulate TTL expiry then another session grabs the name.
        await relay.release_tty_name("kai", "deploy")
        await claim_tty_name(relay, "kai", "kai:bbb2", preferred="deploy")
        # Original session tries to re-reserve — should fail.
        with pytest.raises(ValueError, match="already in use"):
            await rename_tty(relay, "kai", "kai:aaa1", "deploy", preferred="deploy")
