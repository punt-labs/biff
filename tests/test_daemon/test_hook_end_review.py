"""A SessionEnd hook ends the session where it lives, and never makes presence."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.client.connection import ClientIdentity, DaemonClient
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook_daemon import HookDaemon
from biff.session_handle import SessionHandleStore
from tests.test_daemon.claude_seats import LoggingRelay, logging_relay, seat, until

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    import pytest

    from tests.test_daemon.conftest import Harness

_ = logging_relay  # the autouse fixture, imported so pytest finds it here


def _closer(harness: Harness, tmp_path: Path) -> DaemonSessionCloser:
    async def connect(identity: ClientIdentity) -> DaemonClient:
        return await DaemonClient.connect(
            run_dir=harness.run_dir, port=harness.daemon.port, identity=identity
        )

    connector: Callable[[ClientIdentity], Awaitable[DaemonClient]] = connect
    return DaemonSessionCloser(
        HookDaemon(
            connect=connector,
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )


async def test_the_end_reaches_the_relay_the_session_was_opened_on(
    harness: Harness, tmp_path: Path
) -> None:
    s = await seat(harness, "s-moved")
    sender = await harness.connect("s-eric", "eric", client="mcp")
    await sender.request("write", to=f"kai:{s.tty}", message="stranded?")

    # The repo's relay setting changed since the session opened: this hello's
    # directory now names another relay.
    await _closer(harness, tmp_path).end(
        {"session_id": "s-moved", "cwd": "/work/elsewhere"}
    )

    assert await s.row_present() is False
    mailbox = [m.body for m in await s.viewer.fetch_user_inbox("kai")]
    assert mailbox == ["stranded?"]


async def test_an_end_does_not_recreate_presence_that_is_already_gone(
    harness: Harness, tmp_path: Path
) -> None:
    s = await seat(harness, "s-gone")
    await s.client.close()
    await until(lambda: harness.daemon.status().sessions[0].attached == 0)
    await s.viewer.delete_session(s.key)  # the MCP server shut down first
    logins = [e for e in LoggingRelay.events if e.event == "login"]

    await _closer(harness, tmp_path).end({"session_id": "s-gone", "cwd": "/work/repo"})

    assert await s.row_present() is False
    assert [e for e in LoggingRelay.events if e.event == "login"] == logins


async def test_an_end_from_another_run_leaves_the_session_alone(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = await seat(harness, "s-runs", run="4243")
    closer = _closer(harness, tmp_path)

    monkeypatch.setenv("CLAUDE_PID", "4242")  # the exiting run is not the live one
    await closer.end({"session_id": "s-runs", "cwd": "/work/repo"})
    assert await s.row_present() is True

    monkeypatch.setenv("CLAUDE_PID", "4243")  # the live run's own hook ends it
    await closer.end({"session_id": "s-runs", "cwd": "/work/repo"})
    assert await s.row_present() is False
