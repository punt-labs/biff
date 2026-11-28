"""The SessionEnd hook ends its session through biffd and clears its local files."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff._stdlib import active_dir
from biff.client.connection import ClientIdentity, DaemonClient
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.daemon_session_end import DaemonSessionCloser
from biff.plugins.claude_code.hook_daemon import HookDaemon
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


async def test_end_deletes_the_row_and_removes_the_local_files(
    harness: Harness, tmp_path: Path
) -> None:
    unread_dir = tmp_path / "unread"
    handles = SessionHandleStore(tmp_path / "sessions")
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    payload: dict[str, object] = {"session_id": "sess-1", "cwd": str(repo)}
    key = await DaemonSessionOpener(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=unread_dir,
            handles=handles,
            deadline=5.0,
        ),
    ).open(payload)
    assert key is not None
    relay = LocalRelay(tmp_path / "relays" / "repo")
    assert await relay.get_session(key) is not None
    routing = SessionRouting("sess-1")

    await DaemonSessionCloser(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=unread_dir,
            handles=handles,
            deadline=5.0,
        ),
    ).end(payload)

    assert await relay.get_session(key) is None
    assert not routing.unread_path(unread_dir).exists()
    assert handles.read(routing) is None
    assert not (active_dir() / key.replace(":", "-")).exists()


async def test_end_of_a_session_that_is_already_ended_succeeds(
    harness: Harness, tmp_path: Path
) -> None:
    closer = DaemonSessionCloser(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )
    payload: dict[str, object] = {"session_id": "sess-9", "cwd": str(tmp_path)}

    await closer.end(payload)
    await closer.end(payload)


async def test_end_without_a_usable_session_id_asks_nothing(tmp_path: Path) -> None:
    async def refuse(_identity: ClientIdentity) -> DaemonClient:
        msg = "must not connect"
        raise AssertionError(msg)

    closer = DaemonSessionCloser(
        HookDaemon(
            connect=refuse,
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )

    await closer.end({"cwd": str(tmp_path)})


async def test_end_gives_up_at_its_deadline_when_biffd_does_not_answer(
    tmp_path: Path,
) -> None:
    async def never(_identity: ClientIdentity) -> DaemonClient:
        await asyncio.sleep(60)
        raise AssertionError

    closer = DaemonSessionCloser(
        HookDaemon(
            connect=never,
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=0.2,
        ),
    )

    with pytest.raises(TimeoutError):
        await closer.end({"session_id": "sess-1", "cwd": str(tmp_path)})
