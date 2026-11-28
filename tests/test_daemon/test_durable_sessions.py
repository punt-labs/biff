"""A harness session survives a daemon restart, so its unread mail is not stranded."""

from __future__ import annotations

import asyncio
import logging
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.daemon.durable_file import DurableFile
from biff.daemon.harness_records import HarnessRecords
from biff.daemon.resolver import ResolveError
from biff.daemon.server import SESSION_SWEEP_SECONDS, BiffDaemon
from biff.daemon.session_key import SessionKey
from biff.daemon.session_restore import SessionRestore
from biff.daemon.session_sweep import SessionSweeper
from biff.daemon.sessions import SessionTable
from biff.relay import LocalRelay
from biff.session_id import CLAUDE_CODE_ROLE
from tests.test_daemon.claude_seats import ConsumingLogging, seat, until
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    from biff.daemon.attachment import Attachment
    from biff.daemon.relays import ResolvedSession
    from biff.daemon.resolver import SessionResolver
    from biff.frame_client import Hello
    from tests.test_daemon.conftest import Harness


class ScriptedResolver:
    """Resolves as the harness does, except for repositories named to misbehave."""

    def __init__(
        self,
        base: Path,
        *,
        hang: tuple[str, ...] = (),
        boom: tuple[str, ...] = (),
        gone: tuple[str, ...] = (),
        broken: tuple[str, ...] = (),
        down: int = 0,
    ) -> None:
        self._inner = FakeResolver(base)
        self._broken = broken
        self._hang = hang
        self._boom = boom
        self._gone = gone
        self.down = down  # resolves that still fail with a relay error

    async def resolve(self, hello: Hello) -> ResolvedSession:
        name = Path(hello.cwd).name
        if name in self._hang:
            await asyncio.Event().wait()
        if name in self._boom:
            msg = "a resolver bug"
            raise ValueError(msg)
        if name in self._broken:
            msg = f"{hello.cwd}/.punt-labs/biff/config.yaml is not valid YAML"
            raise ResolveError(msg)
        if name in self._gone:
            msg = f"{hello.cwd} is no longer a biff repository"
            raise ResolveError(msg)
        if self.down:
            self.down -= 1
            msg = "the relay is down"
            raise ConnectionError(msg)
        return await self._inner.resolve(hello)


def _records_dir(harness: Harness) -> Path:
    return harness.sessions.root.parent / "harness-sessions"


def _records(harness: Harness) -> list[str]:
    return sorted(p.stem for p in _records_dir(harness).glob("*.json"))


async def _restart(
    harness: Harness,
    resolver: SessionResolver | None = None,
    *,
    after: BiffDaemon | None = None,
    crash: bool = False,
) -> BiffDaemon:
    """Stop the daemon (the harness's, or *after*) and start another over its relays.

    *crash* stops it as a kill does: the listener and relays close, and nothing
    else runs: no hand-back of the outbox, no cleanup of a connection.
    """
    daemon = after or harness.daemon
    if crash:
        await _kill(daemon)
    else:
        await daemon.stop()
    return await _start(harness, resolver)


async def _start(
    harness: Harness,
    resolver: SessionResolver | None = None,
    sweep_interval: float | None = None,
) -> BiffDaemon:
    """Start a daemon over the harness's relays and session store."""
    daemon = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=resolver or FakeResolver(harness.relays_dir),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
        session_sweep_interval=sweep_interval or SESSION_SWEEP_SECONDS,
    )
    await daemon.start()
    return daemon


async def _kill(daemon: BiffDaemon) -> None:
    """Close the listener and the relays and run no shutdown step."""

    def not_noted(_table: SessionTable, _attachment: Attachment) -> None:
        """A killed process does not note that its connections left."""

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(SessionTable, "release", not_noted)
        server = daemon._server  # pyright: ignore[reportPrivateUsage]
        assert server is not None
        server.close()
        await server.wait_closed()
    await daemon._core.pool.close()  # pyright: ignore[reportPrivateUsage]
    # Every task of a killed process stops with it, the sweep and the retry too.
    sweep = daemon._sweep_task  # pyright: ignore[reportPrivateUsage]
    retry = daemon._restore._retry  # pyright: ignore[reportPrivateUsage]
    for task in (sweep, retry):
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _connect(
    harness: Harness,
    daemon: BiffDaemon,
    session_id: str,
    cwd: str = "/work/repo",
    run: str = "a1",
) -> DaemonClient:
    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=daemon.port,
        identity=ClientIdentity(
            client="mcp",
            session_id=session_id,
            main_agent_id=run,
            cwd=cwd,
            user="kai",
        ),
    )
    harness.clients.append(client)
    return client


async def test_unread_session_mail_reaches_the_mailbox_after_a_daemon_restart(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-restart")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="unread for the session")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)

    restarted = await _restart(harness)
    try:
        monkeypatch.setattr("biff.daemon.session_sweep.RETENTION_SECONDS", 0.0)
        await restarted.sweep_sessions()  # the session is dormant past retention

        mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
        assert (mailbox, await s.row_present()) == (["unread for the session"], False)
    finally:
        await restarted.stop()


async def test_a_restored_session_ends_on_request_and_drains_its_mail(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-restart")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="unread for the session")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    restarted = await _restart(harness)
    try:
        held = {x.session_id: x.attached for x in restarted.status().sessions}
        assert held["s-restart"] == 0  # restored, with no connection

        again = await _connect(harness, restarted, "s-restart")
        await again.request("end")

        mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
        assert (mailbox, "s-restart" in _records(harness)) == (
            ["unread for the session"],
            False,
        )
    finally:
        await restarted.stop()


async def test_a_session_is_kept_from_its_hello_to_its_end(harness: Harness) -> None:
    s = await seat(harness, "s-kept")
    assert _records(harness) == ["s-kept"]
    await s.client.request("end")
    assert _records(harness) == []


async def test_a_file_that_is_not_a_record_does_not_stop_the_start(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-good")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    for name, text in (("a", "[]"), ("b", "null"), ("c", '"text"'), ("d", "3")):
        (_records_dir(harness) / f"s-{name}.json").write_text(text)

    restarted = await _restart(harness)
    try:
        held = {x.session_id for x in restarted.status().sessions}
        assert "s-good" in held
    finally:
        await restarted.stop()


async def test_a_hanging_and_a_failing_session_do_not_hold_back_the_good_one(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    for session_id, cwd in (
        ("s-hang", "/work/hang"),
        ("s-boom", "/work/boom"),
        ("s-good", "/work/repo"),
    ):
        client = await _connect(harness, harness.daemon, session_id, cwd)
        await client.close()
    await until(lambda: all(x.attached == 0 for x in harness.daemon.status().sessions))

    with caplog.at_level(logging.INFO, logger="biff.daemon.session_restore"):
        restarted = await _restart(
            harness,
            ScriptedResolver(harness.relays_dir, hang=("hang",), boom=("boom",)),
        )
    try:
        held = {x.session_id for x in restarted.status().sessions}
        assert (
            "s-good" in held,
            "restored 1 of 3 kept sessions, 2 failed" in caplog.messages,
        ) == (
            True,
            True,
        )
    finally:
        await restarted.stop()


async def test_a_record_that_cannot_be_written_refuses_the_hello_and_a_retry_keeps_it(
    harness: Harness,
) -> None:
    blocker = _records_dir(harness)
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("a file where the directory should be")

    with pytest.raises(DaemonError, match="cannot keep the session record"):
        await _connect(harness, harness.daemon, "s-retry")
    blocker.unlink()
    await _connect(harness, harness.daemon, "s-retry")

    assert _records(harness) == ["s-retry"]


async def test_an_end_whose_record_cannot_be_deleted_still_ends(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    s = await seat(harness, "s-stuck")
    directory = _records_dir(harness)
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)  # entries cannot be removed
    try:
        with caplog.at_level(logging.ERROR, logger="biff.daemon.harness_records"):
            await s.client.request("end")
    finally:
        directory.chmod(stat.S_IRWXU)

    assert (await s.row_present(), "could not delete the record" in caplog.text) == (
        False,
        True,
    )


async def test_a_record_whose_session_ended_is_dropped_not_brought_back(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-over")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    await s.viewer.delete_session(s.key)  # the end ran; only the record outlived it

    restarted = await _restart(harness)
    try:
        assert (
            "s-over" in {x.session_id for x in restarted.status().sessions},
            _records(harness),
        ) == (False, [])
    finally:
        await restarted.stop()


async def test_a_session_whose_repository_is_gone_is_dropped(harness: Harness) -> None:
    records = HarnessRecords(_records_dir(harness))
    client = await _connect(harness, harness.daemon, "s-gone", "/work/missing-soon")
    await client.close()
    await until(lambda: all(x.attached == 0 for x in harness.daemon.status().sessions))

    restarted = await _restart(
        harness, ScriptedResolver(harness.relays_dir, gone=("missing-soon",))
    )
    try:
        assert [k.hello.session_id for k in records.load()] == []
    finally:
        await restarted.stop()


async def test_a_session_the_relay_could_not_serve_at_start_is_retried_while_running(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionRestore, "RETRY_DELAYS", (0.05, 0.05, 0.05))
    client = await _connect(harness, harness.daemon, "s-late")
    await client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)

    restarted = await _restart(harness, ScriptedResolver(harness.relays_dir, down=1))
    try:
        await until(
            lambda: "s-late" in {x.session_id for x in restarted.status().sessions}
        )
    finally:
        await restarted.stop()


async def test_a_session_that_fails_to_restore_start_after_start_is_dropped(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionRestore, "MAX_FAILED_STARTS", 2)
    monkeypatch.setattr(SessionRestore, "RETRY_DELAYS", ())
    client = await _connect(harness, harness.daemon, "s-never")
    await client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)

    first = await _restart(harness, ScriptedResolver(harness.relays_dir, down=99))
    after_one = _records(harness)
    second = await _restart(
        harness, ScriptedResolver(harness.relays_dir, down=99), after=first
    )
    try:
        assert (after_one, _records(harness)) == (["s-never"], [])
    finally:
        await second.stop()


async def test_a_restored_session_resumes_its_retention_from_when_it_went_idle(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-old")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    key = SessionKey("s-old", CLAUDE_CODE_ROLE)
    HarnessRecords(_records_dir(harness)).mark(
        key, datetime.now(UTC) - timedelta(days=10)
    )

    restarted = await _restart(harness)
    try:
        monkeypatch.setattr(
            "biff.daemon.session_sweep.RETENTION_SECONDS",
            timedelta(days=3).total_seconds(),
        )
        await restarted.sweep_sessions()
        assert (await s.row_present(), "s-old" in _records(harness)) == (False, False)
    finally:
        await restarted.stop()


async def test_a_session_that_had_a_connection_at_the_restart_gets_a_fresh_retention(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-live")  # the record says a connection is on it

    restarted = await _restart(harness)
    try:
        monkeypatch.setattr(
            "biff.daemon.session_sweep.RETENTION_SECONDS",
            timedelta(days=3).total_seconds(),
        )
        await restarted.sweep_sessions()
        assert await s.row_present() is True
    finally:
        await restarted.stop()


async def test_a_refused_hello_leaves_no_session_for_mail_to_reach(
    harness: Harness,
) -> None:
    sender = await harness.connect("s-eric", "eric", client="mcp")
    directory = _records_dir(harness)
    directory.chmod(stat.S_IRUSR | stat.S_IXUSR)  # no new record can be written
    try:
        with pytest.raises(DaemonError, match="cannot keep the session record"):
            await _connect(harness, harness.daemon, "s-refused")
        await sender.request("write", to="kai", message="for kai, wherever he is")
    finally:
        directory.chmod(stat.S_IRWXU)

    viewer = LocalRelay(harness.factory.built[0])
    rows = [row for row in await viewer.get_sessions() if row.user == "kai"]
    assert rows == []  # nothing lists the refused session, so nothing routes to it


async def test_a_replaced_run_is_the_run_that_comes_back(harness: Harness) -> None:
    first = await _connect(harness, harness.daemon, "s-run", run="a1")
    second = await _connect(harness, harness.daemon, "s-run", run="a2")
    await first.close()
    await second.close()
    await until(lambda: all(x.attached == 0 for x in harness.daemon.status().sessions))

    restarted = await _restart(harness)
    try:
        runs = {x.session_id: x.run for x in restarted.status().sessions}
        assert runs["s-run"] == "a2"
    finally:
        await restarted.stop()


async def test_a_session_with_an_unusable_configuration_is_kept_until_it_is_fixed(
    harness: Harness, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = tmp_path / "work" / "badrepo"
    repo.mkdir(parents=True)
    client = await _connect(harness, harness.daemon, "s-cfg", str(repo))
    await client.close()
    await until(lambda: all(x.attached == 0 for x in harness.daemon.status().sessions))

    broken = await _restart(
        harness, ScriptedResolver(harness.relays_dir, broken=("badrepo",))
    )
    held_while_broken = "s-cfg" in {x.session_id for x in broken.status().sessions}
    kept_while_broken = _records(harness)
    fixed = await _restart(harness, after=broken)
    try:
        assert (
            held_while_broken,
            kept_while_broken,
            "s-cfg" in {x.session_id for x in fixed.status().sessions},
        ) == (False, ["s-cfg"], True)
        assert str(repo) in caplog.text  # the error names where to look
    finally:
        await fixed.stop()


async def test_a_retry_leaves_a_session_that_reconnected_first_alone(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionRestore, "RETRY_DELAYS", (0.5,))
    client = await _connect(harness, harness.daemon, "s-back")
    await client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    key = SessionKey("s-back", CLAUDE_CODE_ROLE)
    records = HarnessRecords(_records_dir(harness))
    records.mark(key, datetime.now(UTC) - timedelta(days=10))
    began = datetime.now(UTC)

    restarted = await _restart(harness, ScriptedResolver(harness.relays_dir, down=1))
    try:
        back = await _connect(harness, restarted, "s-back")  # before the retry
        await back.close()
        await until(lambda: restarted.status().sessions[0].attached == 0)
        retry = restarted._restore._retry  # pyright: ignore[reportPrivateUsage]
        assert retry is not None
        await asyncio.wait_for(retry, 5.0)

        (kept,) = records.load()
        assert kept.dormant_since is not None
        assert kept.dormant_since >= began  # the new disconnect, not the old record
    finally:
        await restarted.stop()


async def test_a_crash_keeps_the_record_and_the_relays_mail_of_a_live_session(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-live")  # a connection is on it when the daemon dies
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="unread in the relay")

    restarted = await _restart(harness, crash=True)
    try:
        monkeypatch.setattr("biff.daemon.session_sweep.RETENTION_SECONDS", 0.0)
        await restarted.sweep_sessions()

        mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
        assert (mailbox, await s.row_present(), "s-live" in _records(harness)) == (
            ["unread in the relay"],
            False,
            False,
        )
    finally:
        await restarted.stop()


async def test_a_crash_loses_the_outbox_but_not_the_mail_still_in_the_relay(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness.factory.relay_type = ConsumingLogging
    s = await seat(harness, "s-held")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="held in the outbox")
    await s.client.request_held("read")  # fetched, never confirmed: only in memory
    await sender.request("write", to=f"kai:{s.tty}", message="still in the relay")

    restarted = await _restart(harness, crash=True)
    try:
        monkeypatch.setattr("biff.daemon.session_sweep.RETENTION_SECONDS", 0.0)
        await restarted.sweep_sessions()

        mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
        assert mailbox == ["still in the relay"]
    finally:
        await restarted.stop()


class HeartbeatDownRelay(LocalRelay):
    """A local relay whose heartbeat can be made to fail: registering then fails."""

    down = False

    async def heartbeat(self, session_key: str, at: datetime | None = None) -> None:
        if type(self).down:
            msg = "the relay stopped answering"
            raise ConnectionError(msg)
        await super().heartbeat(session_key, at)


async def test_a_registration_that_fails_at_restore_keeps_the_dormant_time(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionRestore, "RETRY_DELAYS", ())
    harness.factory.relay_type = HeartbeatDownRelay
    client = await _connect(harness, harness.daemon, "s-keep")
    await client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    key = SessionKey("s-keep", CLAUDE_CODE_ROLE)
    records = HarnessRecords(_records_dir(harness))
    old = datetime.now(UTC) - timedelta(days=2)
    records.mark(key, old)

    monkeypatch.setattr(HeartbeatDownRelay, "down", True)
    restarted = await _restart(harness)
    try:
        (kept,) = records.load()
        assert (kept.dormant_since, kept.failed_starts) == (old, 1)
    finally:
        await restarted.stop()


async def test_registrations_that_keep_failing_at_restore_end_in_a_drop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(SessionRestore, "RETRY_DELAYS", ())
    monkeypatch.setattr(SessionRestore, "MAX_FAILED_STARTS", 3)
    harness.factory.relay_type = HeartbeatDownRelay
    client = await _connect(harness, harness.daemon, "s-doomed")
    await client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)

    monkeypatch.setattr(HeartbeatDownRelay, "down", True)
    daemon = await _restart(harness)
    counts: list[list[str]] = [_records(harness)]
    for _ in range(2):
        daemon = await _restart(harness, after=daemon)
        counts.append(_records(harness))
    await daemon.stop()

    assert counts == [["s-doomed"], ["s-doomed"], []]  # dropped on the third start


async def test_a_first_hello_writes_its_record_once(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    writes: list[Path] = []
    write = DurableFile.replace

    def counting(directory: Path, name: str, text: str) -> None:
        writes.append(directory / name)
        write(directory, name, text)

    monkeypatch.setattr(DurableFile, "replace", staticmethod(counting))

    await _connect(harness, harness.daemon, "s-once")

    assert len(writes) == 1


async def test_a_killed_daemon_sweeps_no_more(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    sweeps: list[int] = []
    real = SessionSweeper.sweep

    async def counting(self: SessionSweeper) -> None:
        sweeps.append(1)
        await real(self)

    monkeypatch.setattr(SessionSweeper, "sweep", counting)
    daemon = await _start(harness, sweep_interval=0.02)
    await until(lambda: len(sweeps) >= 3)  # it sweeps while it lives

    await _kill(daemon)
    after_kill = len(sweeps)
    for _ in range(10):
        await asyncio.sleep(0.02)  # a live sweeper would sweep in this time

    assert len(sweeps) == after_kill
