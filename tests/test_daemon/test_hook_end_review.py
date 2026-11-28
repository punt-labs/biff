"""A SessionEnd hook ends the session where it lives, and never makes presence."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from biff.client.connection import (
    ClientIdentity,
    ConnectionLostError,
    DaemonClient,
    DaemonError,
)
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook_daemon import HookDaemon
from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from tests.test_daemon.claude_seats import LoggingRelay, logging_relay, seat, until

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from tests.test_daemon.conftest import Harness

_ = logging_relay  # the autouse fixture, imported so pytest finds it here


def _hook_daemon(harness: Harness, tmp_path: Path) -> HookDaemon:
    async def connect(identity: ClientIdentity) -> DaemonClient:
        return await DaemonClient.connect(
            run_dir=harness.run_dir, port=harness.daemon.port, identity=identity
        )

    connector: Callable[[ClientIdentity], Awaitable[DaemonClient]] = connect
    return HookDaemon(
        connect=connector,
        unread_dir=tmp_path / "unread",
        handles=SessionHandleStore(tmp_path / "sessions"),
        deadline=5.0,
    )


def _closer(harness: Harness, tmp_path: Path) -> DaemonSessionCloser:
    return DaemonSessionCloser(_hook_daemon(harness, tmp_path))


def _opener(harness: Harness, tmp_path: Path) -> DaemonSessionOpener:
    return DaemonSessionOpener(_hook_daemon(harness, tmp_path))


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


async def test_an_end_with_a_padded_user_override_is_for_the_same_user(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-pad")

    client = await DaemonClient.connect(
        run_dir=harness.run_dir,
        port=harness.daemon.port,
        identity=ClientIdentity(
            client="hook", session_id="s-pad", cwd="/work/repo", user=" kai ", ends=True
        ),
    )
    try:
        await client.request("end")
    finally:
        await client.close()

    assert await s.row_present() is False


async def test_a_session_the_hook_opened_is_ended_only_by_its_own_run(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    payload: dict[str, object] = {"session_id": "s-opened", "cwd": str(repo)}
    monkeypatch.setenv("CLAUDE_PID", "4242")
    key = await _opener(harness, tmp_path).open(payload)
    assert key is not None
    relay = LocalRelay(tmp_path / "relays" / "repo")
    closer = _closer(harness, tmp_path)

    monkeypatch.setenv("CLAUDE_PID", "4243")  # an older process exiting late
    await closer.end(payload)
    assert await relay.get_session(key) is not None

    monkeypatch.setenv("CLAUDE_PID", "4242")
    await closer.end(payload)
    assert await relay.get_session(key) is None


async def test_a_run_admitted_after_the_closer_attached_is_not_ended_by_it(
    harness: Harness,
) -> None:
    s = await seat(harness, "s-race", run="4242")
    closer = await DaemonClient.connect(  # run A's closer has passed the run check
        run_dir=harness.run_dir,
        port=harness.daemon.port,
        identity=ClientIdentity(
            client="hook",
            session_id="s-race",
            cwd="/work/repo",
            main_agent_id="4242",
            ends=True,
        ),
    )
    # claude --resume right after the exit: run B's SessionStart is admitted.
    newer = await harness.connect("s-race", "kai", client="hook", main_agent_id="4243")

    try:
        with pytest.raises((DaemonError, ConnectionLostError)):
            await closer.request("end")
    finally:
        await closer.close()

    assert await s.row_present() is True
    await newer.request("status")  # run B still holds the session
