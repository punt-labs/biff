"""A harness session survives a daemon restart, so its unread mail is not stranded."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.harness_records import HarnessRecords
from biff.daemon.server import BiffDaemon
from biff.daemon.session_restore import SessionRestore
from biff.frame_client import Hello
from tests.test_daemon.claude_seats import seat, until
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    import pytest

    from tests.test_daemon.conftest import Harness


async def _restart(harness: Harness) -> BiffDaemon:
    """Stop the harness daemon and start another over the same relays and store."""
    await harness.daemon.stop()
    restarted = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
    )
    await restarted.start()
    return restarted


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


def _records(harness: Harness) -> list[str]:
    root = harness.sessions.root.parent / "harness-sessions"
    return sorted(p.stem for p in root.glob("*.json"))


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

        again = await DaemonClient.connect(
            run_dir=harness.run_dir,
            port=restarted.port,
            identity=ClientIdentity(
                client="mcp",
                session_id="s-restart",
                main_agent_id="a1",
                cwd="/work/repo",
                user="kai",
            ),
        )
        harness.clients.append(again)
        await again.request("end")

        mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
        kept = _records(harness)
        assert (mailbox, "s-restart" in kept) == (["unread for the session"], False)
    finally:
        await restarted.stop()


async def test_a_session_is_kept_from_its_hello_to_its_end(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-kept")
    assert _records(harness) == ["s-kept"]
    await s.client.request("end")
    assert _records(harness) == []


async def test_a_session_that_cannot_be_restored_keeps_its_record(
    harness: Harness,
) -> None:
    core = harness.daemon._core  # pyright: ignore[reportPrivateUsage]
    records = HarnessRecords(harness.sessions.root.parent / "harness-sessions")
    records.save(
        Hello(
            mac="0" * 64,
            client="mcp",
            session_id="s-gone",
            cwd="/work/missing",  # the resolver refuses this repository
            user="kai",
        )
    )

    await SessionRestore.run(records, core)

    assert (core.sessions.statuses, _records(harness)) == ((), ["s-gone"])
