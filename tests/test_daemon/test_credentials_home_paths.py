"""Every path that resolves a session keeps seeded credentials at the seeded URL."""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.relays import ResolvedSession
from biff.daemon.server import BiffDaemon
from biff.daemon.session_key import SessionKey
from biff.frame_client import Hello
from biff.models import RelayAuth
from biff.relay_seed import RelayConflictError, RelaySeed
from biff.session_id import CLAUDE_CODE_ROLE
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_SEEDED = {"BIFF_RELAY_URL": "nats://relay.invalid:4222", "BIFF_RELAY_TOKEN": "tok-A"}
_KEY = SessionKey("s-keep", CLAUDE_CODE_ROLE)


class _CommittedElsewhere:
    """A repository whose committed configuration now names another relay."""

    def __init__(self, harness: Harness) -> None:
        self._inner = FakeResolver(harness.relays_dir)

    async def resolve(self, hello: Hello) -> ResolvedSession:
        resolved = await self._inner.resolve(hello)
        config = resolved.config.model_copy(
            update={
                "relay_url": "nats://elsewhere:4222",
                "relay_auth": RelayAuth(token="tok-A"),
            }
        )
        return replace(resolved, config=config)


@pytest.fixture
def relay_seed() -> RelaySeed:
    return RelaySeed.of(_SEEDED)


@pytest.fixture(autouse=True)
def _same_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SEEDED.items():
        monkeypatch.setenv(name, value)


async def _start(harness: Harness, *, elsewhere: bool) -> BiffDaemon:
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=(
            _CommittedElsewhere(harness)
            if elsewhere
            else FakeResolver(harness.relays_dir)
        ),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
        relay_seed=RelaySeed.of(_SEEDED),
    )
    await daemon.start()
    return daemon


async def _kept_session(harness: Harness) -> None:
    await harness.daemon.stop()
    honest = await _start(harness, elsewhere=False)
    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=honest.port,
        identity=ClientIdentity(
            client="mcp",
            session_id="s-keep",
            main_agent_id="a1",
            cwd="/work/repo",
            user="kai",
        ),
    )
    await client.close()
    await honest.stop()


async def test_a_restored_session_is_not_given_the_seeded_credentials_elsewhere(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    await _kept_session(harness)
    built = len(harness.factory.built)
    restarted = await _start(harness, elsewhere=True)
    try:
        said = " ".join(record.getMessage() for record in caplog.records)
        assert (
            _KEY in restarted._core.sessions.snapshot(),  # pyright: ignore[reportPrivateUsage]
            len(harness.factory.built) == built,
            "BIFF_RELAY_URL" in said,
            "tok-A" in said,
        ) == (False, True, True, False)
    finally:
        await restarted.stop()


async def test_a_refused_restore_keeps_the_record(harness: Harness) -> None:
    await _kept_session(harness)
    restarted = await _start(harness, elsewhere=True)
    try:
        records = harness.sessions.root.parent / "harness-sessions"
        assert [p.stem for p in records.glob("*.json")] == ["s-keep"]
    finally:
        await restarted.stop()


async def test_one_resolution_step_serves_connections_restores_sweeps_and_spills(
    harness: Harness,
) -> None:
    await harness.daemon.stop()
    daemon = await _start(harness, elsewhere=True)
    try:
        core = daemon._core  # pyright: ignore[reportPrivateUsage]
        hello = Hello(
            mac="0" * 64, client="mcp", session_id="s-x", cwd="/work/repo", user="kai"
        )
        with pytest.raises(RelayConflictError, match="BIFF_RELAY_URL"):
            await core.resolver.resolve(hello)
        sweeper = daemon._sweeper  # pyright: ignore[reportPrivateUsage]
        assert sweeper._resolver is core.resolver  # pyright: ignore[reportPrivateUsage]
    finally:
        await daemon.stop()
