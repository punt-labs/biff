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

import logging
from pathlib import Path
from typing import ClassVar

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
        tty="e5f6a7b8",
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

    async def test_active_lifespan_does_not_register_companion_at_startup(
        self, primary_state_with_companion: ServerState
    ) -> None:
        """Companion registration is deferred to the heartbeat loop.

        Even when ``state.companion`` is pre-populated (legacy path,
        retained so fixtures can probe the registration helper), the
        lifespan must not write a KV row for it. The heartbeat path
        (``_poll_companion_registration``) owns companion registration
        and may overwrite ``state.companion`` with the current roster
        root on its first successful tick.
        """
        state = primary_state_with_companion
        mcp = create_server(state)

        async with Client(FastMCPTransport(mcp)):
            assert state.companion_session_key is not None
            session = await state.relay.get_session(state.companion_session_key)

        assert session is None, "lifespan must not register the companion at startup"


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


class TestPollCompanionRegistration:
    """_poll_companion_registration() registers the human on heartbeat ticks."""

    async def test_registers_companion_when_roster_available(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Companion appears after the heartbeat tick reads the roster."""
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _poll_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-poll-companion",
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

        await _poll_companion_registration(state)

        # _poll_companion_registration mutates via object.__setattr__
        # which mypy can't track — use getattr to bypass narrowing.
        companion = getattr(state, "companion")  # noqa: B009
        assert companion is not None
        session = await state.relay.get_session(companion.session_key)
        assert session is not None
        assert session.tty_name, "companion must have non-empty tty_name"
        assert session.display_name == "Jim"

    async def test_noop_when_no_roster(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No-op when ethos is not configured."""
        from unittest.mock import MagicMock

        from biff.server.app import _poll_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude",
            kind="agent",
            repo_name="_test-poll-companion",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=None)
        )

        await _poll_companion_registration(state)

        assert state.companion is None

    async def test_noop_when_config_user_is_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No companion when the agent is itself the roster root.

        Agent-first identity means ``config.user`` is always
        the agent. When the roster root handle equals ``config.user``,
        the agent is operating without a human at the terminal -- an
        unusual configuration, but valid. No companion is registered.
        """
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _poll_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-agent-is-root",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")

        roster = EthosRoster(
            root=EthosIdentity(handle="claude", display_name="Claude", kind="agent"),
            primary=None,
        )
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=roster)
        )

        await _poll_companion_registration(state)

        assert state.companion is None

    async def test_companion_is_always_roster_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Companion is always ``roster.root`` (the human at the terminal).

        The previous "whichever identity is NOT config.user" rule was a
        workaround for the racy past. With agent-first resolution
        ``config.user`` is always the agent, so ``roster.root`` is
        unambiguously the human.
        """
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _poll_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-roster-root",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")

        roster = EthosRoster(
            root=EthosIdentity(
                handle="jfreeman", display_name="Jim Freeman", kind="human"
            ),
            primary=EthosIdentity(
                handle="claude", display_name="Claude Agento", kind="agent"
            ),
        )
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=roster)
        )

        await _poll_companion_registration(state)

        companion = getattr(state, "companion")  # noqa: B009
        assert companion is not None
        assert companion.user == "jfreeman"
        assert companion.kind == "human"

    async def test_companion_id_is_derived_and_non_volatile(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Companion routing id is a stable derivation, not a random hex.

        The human side must not reproduce the volatile-tty bug:
        the companion id derives deterministically from the agent's
        session_id salted by the human handle — stable across resume and
        distinct from the agent's own id.
        """
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _poll_companion_registration
        from biff.session_id import SessionHint

        config = BiffConfig(
            user="claude",
            display_name="Claude Agento",
            kind="agent",
            repo_name="_test-companion-derive",
        )
        session_id = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        state = create_state(config, tmp_path, tty=session_id, hostname="h", pwd="/")
        roster = EthosRoster(
            root=EthosIdentity(handle="jfreeman", display_name="Jim", kind="human"),
            primary=EthosIdentity(handle="claude", display_name="Claude", kind="agent"),
        )
        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(return_value=roster)
        )

        await _poll_companion_registration(state)

        companion = getattr(state, "companion")  # noqa: B009
        assert companion is not None
        expected = SessionHint.derive_routing_id(session_id, "jfreeman")
        assert companion.tty == expected  # deterministic — non-volatile
        assert companion.tty != session_id  # distinct from the agent id

    async def test_get_ethos_roster_runs_on_worker_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The subprocess call MUST NOT block the event loop (spec invariant 11).

        Pins ``get_ethos_roster`` to a blocking implementation that
        sleeps via ``time.sleep`` (which a coroutine would freeze on).
        If ``_poll_companion_registration`` did not use
        ``asyncio.to_thread``, this test would either hang or run for
        the full sleep duration. With ``to_thread``, an event-loop
        watchdog task can fire during the sleep.
        """
        import time
        from unittest.mock import MagicMock

        from biff.config import EthosIdentity, EthosRoster
        from biff.server.app import _poll_companion_registration

        config = BiffConfig(
            user="claude",
            display_name="Claude",
            kind="agent",
            repo_name="_test-thread",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")

        roster = EthosRoster(
            root=EthosIdentity(handle="jfreeman", display_name="Jim", kind="human"),
            primary=None,
        )

        def _blocking_roster() -> EthosRoster:
            time.sleep(0.1)  # blocks the calling thread, NOT the event loop
            return roster

        monkeypatch.setattr(
            "biff.config.get_ethos_roster", MagicMock(side_effect=_blocking_roster)
        )

        # Watchdog: a concurrent coroutine that increments a counter
        # every 10ms. If the event loop is stalled, the count is low.
        import asyncio

        ticks = 0

        async def _watchdog() -> None:
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        from contextlib import suppress

        watch = asyncio.create_task(_watchdog())
        try:
            await _poll_companion_registration(state)
        finally:
            watch.cancel()
            with suppress(asyncio.CancelledError):
                await watch

        # With to_thread, watchdog fires at least 5 times during the 100ms sleep.
        assert ticks >= 5, f"event loop stalled (only {ticks} watchdog ticks)"


class TestOrgReposRefresh:
    """_refresh_org_repos() updates state.org_repos from relay discovery."""

    async def test_org_repos_refreshed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """New repos discovered after startup appear in state.org_repos."""
        from unittest.mock import AsyncMock

        from biff.server.app import _refresh_org_repos

        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-org-refresh",
            orgs=("punt-labs",),
        )
        state = create_state(
            config,
            tmp_path,
            tty="a1b2c3d4",
            hostname="test-host",
            pwd="/test",
        )
        assert state.org_repos == frozenset()

        # LocalRelay lacks discover_repos_for_org — inject it directly.
        state.relay.discover_repos_for_org = AsyncMock(  # type: ignore[attr-defined]
            return_value=frozenset({"_test-org-refresh", "_test-new-repo"})
        )
        # Make isinstance(state.relay, NatsRelay) pass.
        monkeypatch.setattr("biff.server.app.NatsRelay", type(state.relay))

        await _refresh_org_repos(state)

        assert "_test-new-repo" in state.org_repos
        assert "_test-org-refresh" in state.org_repos

    async def test_noop_without_orgs(self, tmp_path: Path) -> None:
        """No refresh when config.orgs is empty."""
        from biff.server.app import _refresh_org_repos

        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-no-orgs",
        )
        state = create_state(config, tmp_path, tty="a1b2c3d4", hostname="h", pwd="/")

        await _refresh_org_repos(state)

        assert state.org_repos == frozenset()

    async def test_throttled_within_interval(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A second call inside the throttle window makes no relay call.

        This non-critical, stale-tolerant call was running on
        every 60s heartbeat tick and accounted for 70% of captured relay
        timeouts, tripping the shared connection's wedge-detection
        reconnect on behalf of unrelated user-facing calls. Throttling it
        to a slower cadence is the fix — this test is the guarantee that
        the throttle actually suppresses the relay call, not just the
        state update.
        """
        from unittest.mock import AsyncMock

        from biff.server.app import _refresh_org_repos

        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-org-throttle",
            orgs=("punt-labs",),
        )
        state = create_state(
            config, tmp_path, tty="a1b2c3d4", hostname="test-host", pwd="/test"
        )
        discover = AsyncMock(return_value=frozenset({"_test-org-throttle"}))
        state.relay.discover_repos_for_org = discover  # type: ignore[attr-defined]
        monkeypatch.setattr("biff.server.app.NatsRelay", type(state.relay))

        await _refresh_org_repos(state)
        assert discover.call_count == 1

        await _refresh_org_repos(state)
        assert discover.call_count == 1  # still 1 — throttled, no relay call

    async def test_refresh_runs_again_after_interval_elapses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past the throttle window, the next tick refreshes again."""
        import time
        from unittest.mock import AsyncMock

        from biff.server import app as app_module
        from biff.server.app import _refresh_org_repos

        config = BiffConfig(
            user="kai",
            display_name="Kai",
            kind="human",
            repo_name="_test-org-throttle-elapsed",
            orgs=("punt-labs",),
        )
        state = create_state(
            config, tmp_path, tty="a1b2c3d4", hostname="test-host", pwd="/test"
        )
        discover = AsyncMock(return_value=frozenset({"_test-org-throttle-elapsed"}))
        state.relay.discover_repos_for_org = discover  # type: ignore[attr-defined]
        monkeypatch.setattr("biff.server.app.NatsRelay", type(state.relay))

        clock = [1000.0]
        monkeypatch.setattr(time, "monotonic", lambda: clock[0])

        await _refresh_org_repos(state)
        assert discover.call_count == 1

        clock[0] += app_module._ORG_REPOS_REFRESH_INTERVAL + 1.0
        await _refresh_org_repos(state)
        assert discover.call_count == 2


class TestResumeReclaim:
    """Routing on the Claude session_id cures LOST and MISROUTED messages.

    ``register_session`` is invoked with the session_id as the routing token
    (``tty_hex``).  The session key ``{user}:{session_id}`` is stable across
    resume; the display alias ``ttyN`` is reclaimed via the sid hint.
    """

    _KW: ClassVar[dict[str, str]] = {
        "display_name": "Kai",
        "kind": "agent",
        "hostname": "h",
        "pwd": "/",
        "repo": "_test-resume",
    }

    async def test_resume_reclaims_prior_tty_over_lowest_free(
        self, tmp_path: Path
    ) -> None:
        """A resumed session_id reclaims its prior ttyN, not the lowest free."""
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        # Prior life: this session_id last held tty5 (tty1 is the lowest free).
        await relay.set_session_tty_hint("kai", sid, "tty5")

        _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name == "tty5", "resume must reclaim the hinted alias, not tty1"

    async def test_resume_reclaims_alias_still_held_by_same_session(
        self, tmp_path: Path
    ) -> None:
        """The exit->resume overlap must still reclaim the SAME alias.

        Live-verify caught this: on resume our own just-exited session still
        holds its ttyN (the reservation has not released/expired yet), so a
        naive claim treated 'taken' as a foreign collision and reassigned a
        fresh tty. Same-identity takeover reclaims tty16 -> tty16.
        """
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        session_key = f"kai:{sid}"
        # Prior incarnation still holds tty16 AND the hint points at it.
        assert await relay.reserve_tty_name("kai", "tty16", session_key)
        await relay.set_session_tty_hint("kai", sid, "tty16")

        _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name == "tty16", "resume must reclaim its own still-held alias"

    async def test_resume_falls_back_when_prior_tty_taken(self, tmp_path: Path) -> None:
        """Edge case 6: prior ttyN taken by a DIFFERENT session -> lowest-free.

        Uniqueness is preserved — a foreign live holder is never overridden;
        routing is unaffected (the inbox keys on the session_id), only the
        human-facing alias moves.
        """
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        await relay.set_session_tty_hint("kai", sid, "tty1")
        # A DIFFERENT session_id holds tty1 now.
        assert await relay.reserve_tty_name("kai", "tty1", "kai:other-session-id")

        _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name != "tty1"
        assert name == "tty2"

    async def test_poisoned_reclaim_hint_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A team-writable hint with an unsafe value never reaches claim.

        The value flows into a KV key / NATS subject via claim_tty_name, so a
        dotted / namespace-colliding value is validated at the trust boundary
        and the resume falls back to a fresh lowest-free alias — never the
        poisoned name.
        """
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"

        async def _poison(_user: str, _session_id: str) -> str:
            return "evil.name"  # dotted — would inject a subject/KV segment

        monkeypatch.setattr(relay, "get_session_tty_hint", _poison)

        _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name == "tty1", "poisoned hint must be ignored, not reclaimed"
        assert "evil.name" not in await relay.list_reserved_names("kai")

    async def test_reclaim_success_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The SUCCESS path is observable, symmetric with the fallback log."""
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        await relay.set_session_tty_hint("kai", sid, "tty5")

        with caplog.at_level(logging.INFO, logger="biff.server.app"):
            _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name == "tty5"
        assert any(
            f"reclaimed prior alias {name} on resume" in r.message
            and r.levelno == logging.INFO
            for r in caplog.records
        )

    async def test_reclaim_fallback_is_logged(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The fallback path logs the reassignment — the matched pair."""
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        await relay.set_session_tty_hint("kai", sid, "tty1")
        assert await relay.reserve_tty_name("kai", "tty1", "kai:other-session-id")

        with caplog.at_level(logging.INFO, logger="biff.server.app"):
            _, name = await register_session(relay, "kai", sid, **self._KW)

        assert name == "tty2"
        assert any(
            "prior alias tty1 taken on resume; reassigned tty2" in r.message
            and r.levelno == logging.INFO
            for r in caplog.records
        )

    async def test_resume_same_session_id_drains_own_inbox(
        self, tmp_path: Path
    ) -> None:
        """No loss: a message addressed before resume is drained after resume.

        The routing coordinate is the session_id, so the inbox key is
        identical across the exit+resume boundary.
        """
        from biff.models import Message
        from biff.server.app import register_session

        relay = LocalRelay(data_dir=tmp_path)
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        key = f"kai:{sid}"

        # First life.
        _, first_name = await register_session(relay, "kai", sid, **self._KW)
        # A teammate writes to the session while it is alive.
        await relay.deliver(Message(from_user="eric", to_user=key, body="hi"))
        # Clean exit releases the display-alias reservation.
        await relay.release_tty_name("kai", first_name)

        # Resume: same session_id -> same key -> same inbox.
        _, second_name = await register_session(relay, "kai", sid, **self._KW)
        assert second_name == first_name  # reclaimed the alias too

        msgs = await relay.fetch(key)
        assert [m.body for m in msgs] == ["hi"], "resumed session lost its inbox"

    async def test_recycled_tty_n_never_delivers_to_stale_inbox(
        self, tmp_path: Path
    ) -> None:
        """MISROUTED cured: a recycled ttyN resolves to its current holder.

        An old session and a new session reuse the display alias ``tty1``
        over time.  A message sent to the OLD session's inbox must not reach
        the NEW occupant — send-time resolution keys on the session_id.
        """
        from biff.models import Message, UserSession
        from biff.server.tools._session import resolve_tty_name

        relay = LocalRelay(data_dir=tmp_path)
        old_sid = "aaaaaaaa-0000-0000-0000-000000000000"
        new_sid = "bbbbbbbb-1111-1111-1111-111111111111"

        # A message was delivered to the OLD occupant of tty1.
        old_msg = Message(from_user="eric", to_user=f"kai:{old_sid}", body="old")
        await relay.deliver(old_msg)

        # The old session is gone; only the NEW session now carries tty1.
        new_session = UserSession(
            user="kai", tty=new_sid, tty_name="tty1", repo="_test-resume"
        )

        # Send-time resolution of "kai:tty1" yields the current holder.
        resolved = resolve_tty_name([new_session], "kai", "tty1")
        assert resolved is not None
        assert resolved.tty == new_sid

        # The new session's inbox does NOT contain the old message.
        assert await relay.fetch(f"kai:{new_sid}") == []
        # It is still parked on the old key (stranded, not misrouted).
        assert [m.body for m in await relay.fetch(f"kai:{old_sid}")] == ["old"]


class TestReapSentinels:
    """The reaper must not log out the LIVE session under identity routing."""

    _KW: ClassVar[dict[str, str]] = {
        "display_name": "Kai",
        "kind": "agent",
        "hostname": "h",
        "pwd": "/",
        "repo": "_test-reap",
    }

    async def test_reap_skips_our_own_live_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A leftover sentinel for our own key is consumed, not reaped.

        Under identity routing a resumed session carries the SAME key as its
        just-exited incarnation, so reaping the prior sentinel would wipe the
        live session's presence: log it out, release the reclaimed alias, and
        delete its KV row.
        """
        from biff.server import app as app_mod
        from biff.server.app import _write_sentinel, register_session

        config = BiffConfig(
            user="kai", display_name="Kai", kind="agent", repo_name="_test-reap"
        )
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        state = create_state(config, tmp_path, tty=sid, hostname="h", pwd="/")

        # Live registration under our own key (KV row + alias reservation).
        _, tty_name = await register_session(state.relay, "kai", sid, **self._KW)

        # Prior incarnation left a sentinel for the SAME key.
        def _sentinels(_repo: str) -> Path:
            return tmp_path / "sentinels"

        monkeypatch.setattr(app_mod, "sentinel_dir", _sentinels)
        _write_sentinel("_test-reap", state.session_key)

        await app_mod._reap_sentinels(state)

        # Presence survives: KV row intact, alias retained.
        sessions = await state.relay.get_sessions()
        assert any(s.tty == sid for s in sessions), "live session must survive reap"
        assert tty_name in await state.relay.list_reserved_names("kai")
        # Sentinel consumed (no re-processing).
        safe = state.session_key.replace(":", "-")
        assert not (tmp_path / "sentinels" / safe).exists()

    async def test_reap_still_reaps_a_foreign_sentinel(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sentinel for a DIFFERENT (dead) key is still reaped normally."""
        from biff.models import UserSession
        from biff.server import app as app_mod
        from biff.server.app import _write_sentinel, register_session

        config = BiffConfig(
            user="kai", display_name="Kai", kind="agent", repo_name="_test-reap"
        )
        sid = "2f5a1c3e-1b2d-4e5f-8a9b-0c1d2e3f4a5b"
        state = create_state(config, tmp_path, tty=sid, hostname="h", pwd="/")
        await register_session(state.relay, "kai", sid, **self._KW)

        # A dead prior session under a DIFFERENT key.
        dead_sid = "dead0000-0000-0000-0000-000000000000"
        await state.relay.update_session(
            UserSession(user="kai", tty=dead_sid, tty_name="tty9", repo="_test-reap")
        )

        def _sentinels(_repo: str) -> Path:
            return tmp_path / "sentinels"

        monkeypatch.setattr(app_mod, "sentinel_dir", _sentinels)
        _write_sentinel("_test-reap", f"kai:{dead_sid}")

        await app_mod._reap_sentinels(state)

        sessions = await state.relay.get_sessions()
        assert all(s.tty != dead_sid for s in sessions), "dead session must be reaped"
        # Our live session is untouched.
        assert any(s.tty == sid for s in sessions)
