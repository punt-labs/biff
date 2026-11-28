"""A restart without the seed keeps the sessions opened under it, dormant."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.server import BiffDaemon
from biff.daemon.session_key import SessionKey
from biff.relay_seed import NO_RELAY_SETTINGS, RelaySeed
from biff.session_id import CLAUDE_CODE_ROLE
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    from biff.daemon.relays import ResolvedSession
    from biff.frame_client import Hello
    from tests.test_daemon.conftest import Harness

_SEEDED = {"BIFF_RELAY_URL": "nats://relay.invalid:4222", "BIFF_RELAY_TOKEN": "tok-A"}
_KEY = SessionKey("s-keep", CLAUDE_CODE_ROLE)


class _SeedAwareResolver:
    """Puts a session on the seeded relay, or on the repository's own."""

    def __init__(self, base: Path, *, seeded: bool) -> None:
        self._inner = FakeResolver(base / ("seeded" if seeded else "own"))

    async def resolve(self, hello: Hello) -> ResolvedSession:
        return await self._inner.resolve(hello)


@pytest.fixture
def relay_seed() -> RelaySeed:
    return RelaySeed.of(_SEEDED)


@pytest.fixture(autouse=True)
def _same_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in _SEEDED.items():
        monkeypatch.setenv(name, value)


def _records(harness: Harness) -> list[str]:
    root = harness.sessions.root.parent / "harness-sessions"
    return sorted(p.stem for p in root.glob("*.json"))


async def _start(harness: Harness, *, seeded: bool) -> BiffDaemon:
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=_SeedAwareResolver(harness.relays_dir, seeded=seeded),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
        relay_seed=RelaySeed.of(_SEEDED) if seeded else NO_RELAY_SETTINGS,
    )
    await daemon.start()
    return daemon


async def _open(harness: Harness, daemon: BiffDaemon) -> DaemonClient:
    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=daemon.port,
        identity=ClientIdentity(
            client="mcp",
            session_id="s-keep",
            main_agent_id="a1",
            cwd="/work/repo",
            user="kai",
        ),
    )
    harness.clients.append(client)
    return client


async def _seeded_session_then_restart_without_the_seed(
    harness: Harness,
) -> BiffDaemon:
    await harness.daemon.stop()
    seeded = await _start(harness, seeded=True)
    client = await _open(harness, seeded)
    await client.request("plan", message="on the seeded relay")
    await client.close()
    await seeded.stop()
    return await _start(harness, seeded=False)


async def test_a_restart_without_the_seed_keeps_the_record(harness: Harness) -> None:
    unseeded = await _seeded_session_then_restart_without_the_seed(harness)
    try:
        assert _records(harness) == ["s-keep"]
    finally:
        await unseeded.stop()


async def test_a_restart_without_the_seed_does_not_open_the_session(
    harness: Harness,
) -> None:
    unseeded = await _seeded_session_then_restart_without_the_seed(harness)
    try:
        assert _KEY not in unseeded._core.sessions.snapshot()  # pyright: ignore[reportPrivateUsage]
    finally:
        await unseeded.stop()


async def test_a_restart_without_the_seed_says_why_in_the_log(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    unseeded = await _seeded_session_then_restart_without_the_seed(harness)
    try:
        said = " ".join(record.getMessage() for record in caplog.records)
        assert ("s-keep" in said, "dormant" in said, "tok-A" in said) == (
            True,
            True,
            False,
        )
    finally:
        await unseeded.stop()


async def test_the_session_comes_back_when_biffd_is_seeded_again(
    harness: Harness,
) -> None:
    unseeded = await _seeded_session_then_restart_without_the_seed(harness)
    await unseeded.stop()
    again = await _start(harness, seeded=True)
    try:
        assert _KEY in again._core.sessions.snapshot()  # pyright: ignore[reportPrivateUsage]
    finally:
        await again.stop()


async def test_the_record_names_the_seed_it_was_opened_under_never_a_value(
    harness: Harness,
) -> None:
    await harness.daemon.stop()
    seeded = await _start(harness, seeded=True)
    client = await _open(harness, seeded)
    await client.close()
    try:
        (record,) = (harness.sessions.root.parent / "harness-sessions").glob("*.json")
        text = record.read_text()
        assert (
            json.loads(text)["relay_seed"],
            "tok-A" in text,
        ) == (["BIFF_RELAY_TOKEN", "BIFF_RELAY_URL"], False)
    finally:
        await seeded.stop()


async def test_a_restart_with_a_changed_seed_is_flagged_in_health(
    harness: Harness,
) -> None:
    unseeded = await _seeded_session_then_restart_without_the_seed(harness)
    try:
        assert "BIFF_RELAY_TOKEN, BIFF_RELAY_URL" in unseeded.seed_note
    finally:
        await unseeded.stop()
