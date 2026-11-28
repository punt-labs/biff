"""A restart without the seed keeps the sessions opened under it, dormant."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.harness_records import HarnessRecords
from biff.daemon.server import BiffDaemon
from biff.daemon.session_end import RETENTION_SECONDS
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
        ) == (
            "BIFF_RELAY_TOKEN, BIFF_RELAY_URL (relay nats://relay.invalid:4222)",
            False,
        )
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


_OTHER = {"BIFF_RELAY_URL": "nats://other.invalid:4222", "BIFF_RELAY_TOKEN": "tok-B"}


async def _restart_on_another_relay_with_the_same_names(
    harness: Harness,
) -> BiffDaemon:
    await harness.daemon.stop()
    seeded = await _start(harness, seeded=True)
    client = await _open(harness, seeded)
    await client.close()
    await seeded.stop()
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=_SeedAwareResolver(harness.relays_dir, seeded=False),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
        relay_seed=RelaySeed.of(_OTHER),
    )
    await daemon.start()
    return daemon


async def test_a_restart_on_another_relay_with_the_same_names_keeps_the_record(
    harness: Harness,
) -> None:
    other = await _restart_on_another_relay_with_the_same_names(harness)
    try:
        assert (_records(harness), _KEY in other._core.sessions.snapshot()) == (  # pyright: ignore[reportPrivateUsage]
            ["s-keep"],
            False,
        )
    finally:
        await other.stop()


async def test_a_restart_on_another_relay_with_the_same_names_is_flagged(
    harness: Harness,
) -> None:
    other = await _restart_on_another_relay_with_the_same_names(harness)
    try:
        note = other.seed_note
        assert (
            "relay.invalid:4222" in note,
            "other.invalid:4222" in note,
            "tok-A" in note or "tok-B" in note,
        ) == (True, True, False)
    finally:
        await other.stop()


async def _dormant_for(harness: Harness, age: timedelta | None) -> BiffDaemon:
    """Keep a session's record dormant under a seed biffd no longer has, *age* ago."""
    await harness.daemon.stop()
    seeded = await _start(harness, seeded=True)
    client = await _open(harness, seeded)
    await client.close()
    await seeded.stop()
    if age is not None:
        HarnessRecords(
            harness.sessions.root.parent / "harness-sessions", RelaySeed.of(_SEEDED)
        ).mark(_KEY, datetime.now(UTC) - age)
    return await _start(harness, seeded=False)


async def test_a_mismatched_record_dormant_past_the_row_lifetime_is_dropped(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    expired = await _dormant_for(harness, timedelta(seconds=RETENTION_SECONDS + 60))
    try:
        said = " ".join(record.getMessage() for record in caplog.records)
        assert (_records(harness), "s-keep" in said and "dropping" in said) == (
            [],
            True,
        )
    finally:
        await expired.stop()


async def test_a_mismatched_record_dormant_for_less_is_kept(harness: Harness) -> None:
    young = await _dormant_for(harness, timedelta(seconds=60))
    try:
        assert _records(harness) == ["s-keep"]
    finally:
        await young.stop()


async def test_the_first_sight_of_a_mismatched_record_starts_its_clock(
    harness: Harness,
) -> None:
    first = await _dormant_for(harness, None)
    try:
        (kept,) = HarnessRecords(
            harness.sessions.root.parent / "harness-sessions", RelaySeed.of(_SEEDED)
        ).load()
        assert (_records(harness), kept.dormant_since is not None) == (
            ["s-keep"],
            True,
        )
    finally:
        await first.stop()


_KEY_BYTES = b"k" * 32


async def test_a_restart_on_the_same_host_with_another_token_keeps_the_record(
    harness: Harness,
) -> None:
    await harness.daemon.stop()
    mine = RelaySeed.for_daemon(_SEEDED, _KEY_BYTES)
    first = await _start_with(harness, mine, "seeded")
    client = await _open(harness, first)
    await client.close()
    await first.stop()
    theirs = RelaySeed.for_daemon({**_SEEDED, "BIFF_RELAY_TOKEN": "tok-B"}, _KEY_BYTES)
    second = await _start_with(harness, theirs, "other")
    try:
        assert (_records(harness), "credential" in second.seed_note) == (
            ["s-keep"],
            True,
        )
    finally:
        await second.stop()


async def _start_with(harness: Harness, seed: RelaySeed, where: str) -> BiffDaemon:
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=_SeedAwareResolver(harness.relays_dir, seeded=where == "seeded"),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
        relay_seed=seed,
    )
    await daemon.start()
    return daemon


async def test_a_restart_with_only_the_path_changed_keeps_the_record_and_says_so(
    harness: Harness,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    caplog.set_level(logging.INFO)
    await harness.daemon.stop()
    path_a = {"BIFF_RELAY_URL": "wss://relay.invalid:443/k-SECRET-PATH-7f3a"}
    monkeypatch.delenv("BIFF_RELAY_TOKEN")
    monkeypatch.setenv("BIFF_RELAY_URL", path_a["BIFF_RELAY_URL"])
    first = await _start_with(
        harness, RelaySeed.for_daemon(path_a, _KEY_BYTES), "seeded"
    )
    client = await _open(harness, first)
    await client.close()
    await first.stop()
    path_b = {"BIFF_RELAY_URL": "wss://relay.invalid:443/k-OTHER-PATH-91cc"}
    second = await _start_with(
        harness, RelaySeed.for_daemon(path_b, _KEY_BYTES), "other"
    )
    try:
        said = " ".join(r.getMessage() for r in caplog.records) + second.seed_note
        assert (
            _records(harness),
            "credentials or path differs" in second.seed_note,
            "secret-path" in said.casefold() or "other-path" in said.casefold(),
        ) == (["s-keep"], True, False)
    finally:
        await second.stop()
