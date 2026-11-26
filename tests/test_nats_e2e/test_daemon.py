"""biffd over a real NATS server: repo keying and mail that is never lost.

The daemon here builds its relays with its default factory, so each session gets
a real ``NatsRelay``.  ``NatsRelay.fetch`` acks what it pulls and the work queue
deletes it, so only the daemon's outbox keeps mail the client did not receive.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, cast

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.connection import ConnectionHandler
from biff.daemon.relays import ResolvedSession
from biff.daemon.server import BiffDaemon
from biff.models import BiffConfig
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from biff.frame_client import Hello

pytestmark = pytest.mark.nats_local

_WAIT = 20.0


class _Resolver:
    """One NATS server, many repos: the repo is the last part of the cwd."""

    def __init__(self, url: str, base: Path) -> None:
        self._url = url
        self._base = base

    async def resolve(self, hello: Hello) -> ResolvedSession:
        repo = hello.cwd.rsplit("/", 1)[-1]
        config = BiffConfig(user=hello.user, repo_name=repo, relay_url=self._url)
        return ResolvedSession(
            config=config, data_dir=self._base / repo, repo_root=None
        )


class _Daemon:
    def __init__(self, daemon: BiffDaemon, run_dir: RunDir) -> None:
        self.daemon = daemon
        self.run_dir = run_dir
        self.clients: list[DaemonClient] = []

    async def connect(self, session_id: str, user: str, repo: str) -> DaemonClient:
        client = await DaemonClient.connect(
            run_dir=self.run_dir,
            port=self.daemon.port,
            identity=ClientIdentity(
                client="cli", session_id=session_id, cwd=f"/work/{repo}", user=user
            ),
            request_timeout=_WAIT,
        )
        self.clients.append(client)
        return client


@pytest.fixture
async def nats_daemon(nats_server: str, tmp_path: Path) -> AsyncIterator[_Daemon]:
    run_dir = RunDir(tmp_path / "run")
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        resolver=_Resolver(nats_server, tmp_path / "data"),
        read_budget=4000,
        attach_timeout=_WAIT,
        request_timeout=_WAIT,
    )
    await daemon.start()
    env = _Daemon(daemon, run_dir)
    yield env
    for client in env.clients:
        await client.close()
    await daemon.stop()


def _bodies(result: object) -> list[str]:
    data = cast("list[dict[str, str]]", cast("dict[str, object]", result)["data"])
    return [row["body"] for row in data]


def _more(result: object) -> int:
    return cast("int", cast("dict[str, object]", result).get("more", 0))


async def test_two_repos_on_one_nats_server_keep_their_mail_apart(
    nats_daemon: _Daemon,
) -> None:
    kai = await nats_daemon.connect("s-kai", "kai", "_test-daemon-a")
    eric = await nats_daemon.connect("s-eric", "eric", "_test-daemon-b")
    assert nats_daemon.daemon.status().relay_connections == 2
    await eric.request("write", to="kai", message="for repo b only")
    assert _bodies(await kai.request("read")) == []


async def test_two_sessions_of_one_repo_share_one_nats_relay(
    nats_daemon: _Daemon,
) -> None:
    kai = await nats_daemon.connect("s-kai", "kai", "_test-daemon-a")
    eric = await nats_daemon.connect("s-eric", "eric", "_test-daemon-a")
    assert nats_daemon.daemon.status().relay_connections == 1
    await eric.request("write", to="kai", message="same repo")
    assert _bodies(await kai.request("read")) == ["same repo"]


async def test_mail_the_client_never_received_survives_on_nats(
    nats_daemon: _Daemon, monkeypatch: pytest.MonkeyPatch
) -> None:
    kai = await nats_daemon.connect("s-kai", "kai", "_test-daemon-a")
    eric = await nats_daemon.connect("s-eric", "eric", "_test-daemon-a")
    sent = [f"letter {n}" for n in range(3)]
    for body in sent:
        await eric.request("write", to="kai", message=body)

    attempted = asyncio.Event()

    async def failing_send(_ws: object, _frame: object) -> bool:
        attempted.set()  # the daemon fetched, and its reply was lost in transit
        return False

    with monkeypatch.context() as patch:
        patch.setattr(ConnectionHandler, "_send", staticmethod(failing_send))
        for _ in range(2):
            attempted.clear()
            lost = asyncio.create_task(kai.request("read"))
            await asyncio.wait_for(attempted.wait(), _WAIT)
            lost.cancel()
            await asyncio.gather(lost, return_exceptions=True)
    assert _bodies(await kai.request("read")) == sent
    assert _bodies(await kai.request("read")) == []


async def test_a_batch_limit_loses_nothing_on_nats(nats_daemon: _Daemon) -> None:
    kai = await nats_daemon.connect("s-kai", "kai", "_test-daemon-a")
    eric = await nats_daemon.connect("s-eric", "eric", "_test-daemon-a")
    sent = [f"message {n:03d} " + "x" * 200 for n in range(25)]
    for body in sent:
        await eric.request("write", to="kai", message=body)
    received: list[str] = []
    for _ in range(25):
        result = await kai.request("read")
        received += _bodies(result)
        if not _more(result):
            break
    assert received == sent
