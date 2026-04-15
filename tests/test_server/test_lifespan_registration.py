"""Regression tests for session registration order (biff-dzqc).

Guards the claim-then-write invariant established by
:func:`biff.server.app.register_session`.  The v1.8.0 defect shipped
a two-write pattern that left KV rows with empty ``tty_name`` when
anything failed between the two writes.  These tests exercise the
lifespan startup path and the companion registration helper against
``LocalRelay`` to assert the invariant holds.

See ``.tmp/root-cause-dual-session-companion.md`` §4 for the original
test surface design.
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
