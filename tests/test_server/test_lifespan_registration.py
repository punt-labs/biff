"""Tier-1 invariant tests for session registration.

These tests exercise :func:`biff.server.app.register_session` and the
companion registration helper against ``LocalRelay`` and assert the
post-condition that ``tty_name`` is non-empty on every written row.

They are NOT regression guards for the v1.8.0 two-write defect: the
defect was a narrow window between a first KV write (empty tty_name)
and a second KV write (populated tty_name) where a NATS I/O error
could leave a half-formed row behind.  ``LocalRelay`` writes to an
in-memory dict and cannot fail between the two calls, so the v1.8.0
bug could not have failed these tests.

The actual regression guard for the two-write pattern lives at
``tests/test_nats_e2e/test_dual_session_lifespan.py`` (tier-3b) where
real NATS I/O can fail between writes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import FastMCPTransport

from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.server.app import _register_companion, create_server
from biff.server.state import CompanionSession, ServerState, create_state


@pytest.fixture
def primary_state_with_companion(tmp_path: Path) -> ServerState:
    """A ServerState with both primary (claude) and companion (jfreeman)."""
    config = BiffConfig(
        user="claude",
        display_name="Claude Agento",
        kind="agent",
        repo_name="_test-lifespan",
    )
    companion = CompanionSession(
        user="jfreeman",
        display_name="Jim Freeman",
        kind="human",
        tty="e5f6g7h8",
    )
    return create_state(
        config,
        tmp_path,
        tty="a1b2c3d4",
        hostname="test-host",
        pwd="/test",
        companion=companion,
    )


class TestLifespanRegistration:
    """The active lifespan writes fully formed KV rows for primary and companion."""

    async def test_active_lifespan_registers_primary_with_tty_name(
        self, tmp_path: Path
    ) -> None:
        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-lifespan-primary",
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
        )
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)):
            session = await state.relay.get_session(state.session_key)

        assert session is not None
        assert session.tty_name, "primary row must have non-empty tty_name"
        assert session.user == "kai"

    async def test_active_lifespan_registers_companion_with_tty_name(
        self, primary_state_with_companion: ServerState
    ) -> None:
        state = primary_state_with_companion
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)):
            assert state.companion_session_key is not None
            session = await state.relay.get_session(state.companion_session_key)

        assert session is not None
        assert session.tty_name, "companion row must have non-empty tty_name"
        assert session.display_name == "Jim Freeman"


class TestRegisterCompanion:
    """_register_companion() writes a single fully formed KV row."""

    async def test_writes_tty_name(
        self, primary_state_with_companion: ServerState
    ) -> None:
        state = primary_state_with_companion

        await _register_companion(state)

        assert state.companion_session_key is not None
        session = await state.relay.get_session(state.companion_session_key)
        assert session is not None
        assert session.tty_name, "companion row must have non-empty tty_name"

    async def test_atomic_under_claim_failure(
        self,
        primary_state_with_companion: ServerState,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A claim failure leaves no half-written row behind."""
        state = primary_state_with_companion

        async def _boom(*_args: object, **_kwargs: object) -> str:
            msg = "simulated claim failure"
            raise RuntimeError(msg)

        monkeypatch.setattr("biff.server.app.claim_tty_name", _boom)

        with pytest.raises(RuntimeError, match="simulated claim failure"):
            await _register_companion(state)

        # Invariant: either no row, or a row with tty_name set.
        # claim-then-write means no row at all.
        assert state.companion_session_key is not None
        session = await state.relay.get_session(state.companion_session_key)
        if session is not None:
            assert session.tty_name, (
                "companion row present without tty_name — half-written state"
            )


class TestRegisterSessionHelper:
    """register_session() returns the written row with tty_name set."""

    async def test_returns_session_with_tty_name(self, tmp_path: Path) -> None:
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        session, tty_name = await register_session(
            relay,
            "kai",
            "a1b2c3d4",
            display_name="Kai",
            kind="human",
            hostname="test-host",
            pwd="/test",
            repo="_test-register",
        )

        assert tty_name
        assert session.tty_name == tty_name
        assert session.user == "kai"
        assert session.display_name == "Kai"

        stored = await relay.get_session("kai:a1b2c3d4")
        assert stored is not None
        assert stored.tty_name == tty_name

    async def test_releases_stale_tty_reservation_on_restart(
        self, tmp_path: Path
    ) -> None:
        """A prior crash leaving an orphan tty_name reservation is cleaned up.

        Seeds a KV row with an outdated ``tty_name`` plus a matching lockfile
        reservation, then invokes ``register_session`` for the same key.
        The pre-existing reservation must be released so repeated crash-restart
        cycles cannot accumulate orphan names.
        """
        from datetime import UTC, datetime

        from biff.models import UserSession
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        session_key = "kai:a1b2c3d4"
        stale_name = "tty7"
        # Seed the KV row and a real reservation for the stale name.
        seeded = UserSession(
            user="kai",
            tty="a1b2c3d4",
            tty_name=stale_name,
            display_name="Kai",
            kind="human",
            hostname="old-host",
            pwd="/old",
            repo="_test-register",
            last_active=datetime.now(UTC),
        )
        await relay.update_session(seeded)
        ok = await relay.reserve_tty_name("kai", stale_name, session_key)
        assert ok, "seeding the stale reservation must succeed"

        _, new_name = await register_session(
            relay,
            "kai",
            "a1b2c3d4",
            display_name="Kai",
            kind="human",
            hostname="test-host",
            pwd="/test",
            repo="_test-register",
        )

        reserved = await relay.list_reserved_names("kai")
        assert stale_name not in reserved, (
            f"stale reservation {stale_name} must be released on re-register"
        )
        assert new_name in reserved, "newly claimed name must remain reserved"

    async def test_preserves_reservation_owned_by_foreign_session(
        self, tmp_path: Path
    ) -> None:
        """A stale row whose tty_name has been reclaimed by another session.

        Seeds a KV row for ``kai:a1b2c3d4`` pointing at ``tty7``, then has
        a DIFFERENT session (``kai:deadbeef``) hold the reservation for
        ``tty7``.  ``register_session`` for ``kai:a1b2c3d4`` must not
        release the reservation owned by ``kai:deadbeef`` — the foreign
        session is live and still needs its name.
        """
        from datetime import UTC, datetime

        from biff.models import UserSession
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        foreign_key = "kai:deadbeef"
        contested_name = "tty7"
        seeded = UserSession(
            user="kai",
            tty="a1b2c3d4",
            tty_name=contested_name,
            display_name="Kai",
            kind="human",
            hostname="old-host",
            pwd="/old",
            repo="_test-register",
            last_active=datetime.now(UTC),
        )
        await relay.update_session(seeded)
        # Foreign session owns the reservation now.
        ok = await relay.reserve_tty_name("kai", contested_name, foreign_key)
        assert ok

        await register_session(
            relay,
            "kai",
            "a1b2c3d4",
            display_name="Kai",
            kind="human",
            hostname="test-host",
            pwd="/test",
            repo="_test-register",
        )

        owner = await relay.get_tty_reservation_owner("kai", contested_name)
        assert owner == foreign_key, (
            "foreign-owned reservation must not be revoked by re-register"
        )


class TestLateCompanionRegistration:
    """_try_late_companion_registration() recovers from startup race."""

    async def test_registers_companion_when_roster_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Companion appears after first heartbeat when ethos was slow."""
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _try_late_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-late-companion",
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
        )
        assert state.companion is None

        roster = EthosRoster(
            root=EthosIdentity(handle="jfreeman", display_name="Jim", kind="human"),
            primary=EthosIdentity(handle="claude", display_name="Claude", kind="agent"),
        )
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=roster)
        )

        await _try_late_companion_registration(state)

        # _try_late_companion_registration mutates via object.__setattr__
        # which mypy can't track — use getattr to bypass narrowing.
        companion = getattr(state, "companion")  # noqa: B009
        assert companion is not None
        session = await state.relay.get_session(companion.session_key)
        assert session is not None
        assert session.tty_name, "late companion must have non-empty tty_name"
        assert session.display_name == "Jim"

    async def test_noop_when_no_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-op when ethos is not configured."""
        from unittest.mock import MagicMock

        from biff.server.app import _try_late_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude",
            kind="agent",
            repo_name="_test-late-companion",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=None)
        )

        await _try_late_companion_registration(state)

        assert state.companion is None
