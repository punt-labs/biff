"""The SessionStart hook opens its session through biffd, never in-process."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from biff.session_routing import SessionRouting

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from tests.test_daemon.conftest import Harness


def _connector(
    harness: Harness,
) -> Callable[[ClientIdentity], Awaitable[DaemonClient]]:
    async def connect(identity: ClientIdentity) -> DaemonClient:
        return await DaemonClient.connect(
            run_dir=harness.run_dir, port=harness.daemon.port, identity=identity
        )

    return connect


async def test_open_registers_the_row_and_writes_the_local_files(
    harness: Harness, tmp_path: Path
) -> None:
    unread_dir = tmp_path / "unread"
    handles = SessionHandleStore(tmp_path / "sessions")
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    opener = DaemonSessionOpener(
        connect=_connector(harness),
        unread_dir=unread_dir,
        handles=handles,
        deadline=5.0,
    )

    key = await opener.open({"session_id": "sess-1", "cwd": str(repo)})

    assert key is not None
    row = await LocalRelay(tmp_path / "relays" / "repo").get_session(key)
    assert row is not None and row.tty_name
    routing = SessionRouting("sess-1")
    unread = json.loads(routing.unread_path(unread_dir).read_text())
    assert unread["tty_name"] == row.tty_name
    assert unread["count"] == 0
    assert handles.read(routing) == key


async def test_open_without_a_usable_session_id_does_nothing(
    harness: Harness, tmp_path: Path
) -> None:
    opener = DaemonSessionOpener(
        connect=_connector(harness),
        unread_dir=tmp_path / "unread",
        handles=SessionHandleStore(tmp_path / "sessions"),
        deadline=5.0,
    )

    assert await opener.open({"cwd": str(tmp_path)}) is None
    assert not (tmp_path / "unread").exists()


async def test_open_gives_up_at_its_deadline_when_biffd_does_not_answer(
    tmp_path: Path,
) -> None:
    async def never(_identity: ClientIdentity) -> DaemonClient:
        await asyncio.sleep(60)
        raise AssertionError

    opener = DaemonSessionOpener(
        connect=never,
        unread_dir=tmp_path / "unread",
        handles=SessionHandleStore(tmp_path / "sessions"),
        deadline=0.2,
    )

    with pytest.raises(TimeoutError):
        await opener.open({"session_id": "sess-1", "cwd": str(tmp_path)})
