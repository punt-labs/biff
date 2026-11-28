"""Retention ends a harness session nobody is attached to; a restart keeps it.

``docs/specs/claude-session-end.tex``: ``ExpireClaude`` (a dormant session aged to
retention ends like an own end), the retention obligations, ``shownDormant`` and
``DaemonRestart``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.daemon.server import BiffDaemon
from tests.test_daemon.claude_seats import (
    ConsumingLogging,
    LoggingRelay,
    logging_relay,
    seat,
    until,
)
from tests.test_daemon.conftest import FakeResolver

if TYPE_CHECKING:
    from tests.test_daemon.conftest import Harness

_ = logging_relay


async def _sweep(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, retention: float
) -> None:
    monkeypatch.setattr("biff.daemon.session_sweep.RETENTION_SECONDS", retention)
    await harness.daemon.sweep_sessions()


def _is_dormant(harness: Harness) -> bool:
    return harness.daemon.status().sessions[0].attached == 0


async def test_a_session_nobody_is_attached_to_expires_at_retention(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-crashed")
    await s.client.close()  # a crash: the connection goes, no end is sent
    await until(lambda: _is_dormant(harness))
    await _sweep(harness, monkeypatch, 3600.0)
    assert await s.row_present() is True  # retention not yet spent
    await _sweep(harness, monkeypatch, 0.0)
    assert (await s.row_present(), await s.owner()) == (False, None)
    assert harness.daemon.status().sessions == ()
    logouts = [e.session_key for e in LoggingRelay.events if e.event == "logout"]
    assert logouts == [s.key]


async def test_a_session_with_a_connection_never_expires(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-alive")
    await _sweep(harness, monkeypatch, 0.0)
    assert await s.row_present() is True
    assert len(harness.daemon.status().sessions) == 1


async def test_a_reattach_restarts_the_retention(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-back")
    await s.client.close()
    await until(lambda: _is_dormant(harness))
    again = await harness.connect("s-back", "kai", client="mcp", main_agent_id="a1")
    await _sweep(harness, monkeypatch, 0.0)
    assert await s.row_present() is True
    await again.request("end")
    assert await s.row_present() is False


async def test_an_expiry_the_relay_refuses_is_tried_again_by_the_next_sweep(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-flaky")
    await s.client.close()
    await until(lambda: _is_dormant(harness))
    real = LoggingRelay.delete_session
    failures = iter([ConnectionError("relay down")])

    async def flaky(self: LoggingRelay, session_key: str) -> None:
        if (exc := next(failures, None)) is not None:
            raise exc
        await real(self, session_key)

    monkeypatch.setattr(LoggingRelay, "delete_session", flaky)
    await _sweep(harness, monkeypatch, 0.0)
    assert len(harness.daemon.status().sessions) == 1  # the record stays
    await _sweep(harness, monkeypatch, 0.0)
    assert harness.daemon.status().sessions == ()
    assert await s.row_present() is False


async def test_a_reply_nobody_confirmed_is_not_shown_to_a_session_with_no_client(
    harness: Harness,
) -> None:
    harness.factory.relay_type = ConsumingLogging
    s = await seat(harness, "s-dormant")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="held")
    await s.client.request_held("read")  # handed over, never confirmed
    await s.client.close()
    await until(lambda: _is_dormant(harness))
    again = await harness.connect("s-dormant", "kai", client="mcp", main_agent_id="a1")
    reply = cast("dict[str, list[dict[str, str]]]", await again.request("read"))
    assert [row["body"] for row in reply["data"]] == ["held"]  # still unread


@pytest.mark.parametrize("consuming", [False, True])
async def test_a_daemon_restart_keeps_the_mail_a_read_held(
    harness: Harness, consuming: bool
) -> None:
    harness.factory.relay_type = ConsumingLogging if consuming else LoggingRelay
    s = await seat(harness, "s-restart")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="in flight")
    await s.client.request_held("read")
    await harness.daemon.stop()
    revived = BiffDaemon(
        run_dir=harness.run_dir,
        port=0,
        sessions=harness.sessions,
        resolver=FakeResolver(harness.relays_dir),
        relay_factory=harness.factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
    )
    await revived.start()
    try:
        # The session is restored from its record: dormant, before any hello.
        restored = {x.session_id: x.attached for x in revived.status().sessions}
        assert restored["s-restart"] == 0
        client = await DaemonClient.connect(
            run_dir=harness.run_dir,
            port=revived.port,
            identity=ClientIdentity(
                client="mcp",
                session_id="s-restart",
                main_agent_id="a1",
                cwd="/work/repo",
                user="kai",
            ),
        )
        reply = cast("dict[str, list[dict[str, str]]]", await client.request("read"))
        bodies = [row["body"] for row in reply["data"]]
        # A consuming relay gave the held batch up at fetch, so stopping files it
        # back; a relay that keeps mail never lost it.
        assert bodies == ["in flight"]
        await client.close()
    finally:
        await revived.stop()


async def test_an_expired_session_answers_end_with_a_refusal(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-gone")
    again = await harness.connect("s-gone", "kai", client="mcp", main_agent_id="a1")
    await s.client.close()
    await again.close()
    await until(lambda: _is_dormant(harness))
    await _sweep(harness, monkeypatch, 0.0)
    fresh = await harness.connect("s-gone", "kai", client="mcp", main_agent_id="a1")
    await fresh.request("end")  # a new session under the same id ends normally
    with pytest.raises(DaemonError):
        await fresh.request("read")
