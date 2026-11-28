"""The SessionStart hook opens its session through biffd, never in-process."""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from biff._stdlib import active_dir
from biff.client.connection import ClientIdentity, DaemonClient, DaemonError
from biff.frame_reply import ErrorCode
from biff.plugins.claude_code.daemon_session import DaemonSessionOpener
from biff.plugins.claude_code.hook_daemon import HookDaemon
from biff.relay import LocalRelay
from biff.session_handle import SessionHandleStore
from biff.session_registry import register_session
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
        HookDaemon(
            connect=_connector(harness),
            unread_dir=unread_dir,
            handles=handles,
            deadline=5.0,
        ),
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
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
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
        HookDaemon(
            connect=never,
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=0.2,
        ),
    )

    with pytest.raises(TimeoutError):
        await opener.open({"session_id": "sess-1", "cwd": str(tmp_path)})


async def test_the_tty_address_the_hook_opened_is_the_one_the_server_attaches_with(
    harness: Harness, tmp_path: Path
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    opener = DaemonSessionOpener(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )
    key = await opener.open({"session_id": "sess-1", "cwd": str(repo)})
    assert key is not None
    relay = LocalRelay(tmp_path / "relays" / "repo")
    opened = await relay.get_session(key)
    assert opened is not None

    # The MCP server registers in-process, claiming the alias this session
    # last held, as a resumed session does.
    _, attached = await register_session(
        relay,
        "kai",
        key.partition(":")[2],
        display_name="",
        kind="agent",
        hostname="h",
        pwd=str(repo),
        repo="test",
    )

    assert attached == opened.tty_name


async def test_open_records_the_repo_common_root_in_the_active_marker(
    harness: Harness, tmp_path: Path
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    git = [
        "git",
        "-C",
        str(repo),
        "-c",
        "user.email=t@e",
        "-c",
        "user.name=t",
        "-c",
        "commit.gpgsign=false",
    ]
    subprocess.run([*git, "init", "-q"], check=True)  # noqa: S603
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "x"], check=True)  # noqa: S603
    linked = tmp_path / "linked"
    subprocess.run([*git, "worktree", "add", "-q", str(linked)], check=True)  # noqa: S603
    opener = DaemonSessionOpener(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )

    # From a linked worktree the marker still names the main checkout, the one
    # root every worktree of the repo shares, as collision detection compares.
    key = await opener.open({"session_id": "sess-1", "cwd": str(linked)})

    assert key is not None
    marker = active_dir() / key.replace(":", "-")
    assert marker.read_text().splitlines()[2] == str(repo.resolve())


async def test_a_partial_open_ends_the_session_it_started(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)

    def broken(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(
        "biff.plugins.claude_code.daemon_session.write_unread_file", broken
    )
    handles = SessionHandleStore(tmp_path / "sessions")
    opener = DaemonSessionOpener(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=handles,
            deadline=5.0,
        ),
    )

    with pytest.raises(OSError, match="disk full"):
        await opener.open({"session_id": "sess-1", "cwd": str(repo)})

    # The daemon has no session without the files that go with it.
    assert harness.daemon.status().sessions == ()
    assert handles.read(SessionRouting("sess-1")) is None
    assert not active_dir().exists() or not list(active_dir().iterdir())


async def test_a_refused_status_request_ends_the_session_the_hello_made(
    harness: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    real_request = DaemonClient.request

    async def refuse_status(
        self: DaemonClient, method: str, **params: object
    ) -> object:
        if method == "status":
            raise DaemonError(ErrorCode.INTERNAL, "biffd could not answer status")
        return await real_request(self, method, **params)  # type: ignore[arg-type]

    monkeypatch.setattr(DaemonClient, "request", refuse_status)
    opener = DaemonSessionOpener(
        HookDaemon(
            connect=_connector(harness),
            unread_dir=tmp_path / "unread",
            handles=SessionHandleStore(tmp_path / "sessions"),
            deadline=5.0,
        ),
    )

    with pytest.raises(DaemonError):
        await opener.open({"session_id": "sess-1", "cwd": str(repo)})

    assert harness.daemon.status().sessions == ()  # biffd keeps no half-opened one
