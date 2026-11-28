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
from biff.daemon.harness_records import HarnessRecords
from biff.daemon.resolver import ResolveError
from biff.daemon.server import BiffDaemon
from biff.daemon.session_key import SessionKey
from biff.daemon.session_restore import SessionRestore
from biff.session_id import CLAUDE_CODE_ROLE
from tests.test_daemon.claude_seats import seat, until
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
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
        down: int = 0,
    ) -> None:
        self._inner = FakeResolver(base)
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
) -> BiffDaemon:
    """Stop the daemon (the harness's, or *after*) and start another over its relays."""
    await (after or harness.daemon).stop()
    restarted = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=resolver or FakeResolver(harness.relays_dir),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
        attach_timeout=0.3,
    )
    await restarted.start()
    return restarted


async def _connect(
    harness: Harness, daemon: BiffDaemon, session_id: str, cwd: str = "/work/repo"
) -> DaemonClient:
    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=daemon.port,
        identity=ClientIdentity(
            client="mcp",
            session_id=session_id,
            main_agent_id="a1",
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
