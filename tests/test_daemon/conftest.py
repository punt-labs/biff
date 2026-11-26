"""Fixtures: a running daemon on an ephemeral port over local relays."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, final

import pytest

from biff.client.connection import ClientIdentity, DaemonClient
from biff.daemon.relays import ResolvedSession
from biff.daemon.resolver import ResolveError
from biff.daemon.server import BiffDaemon
from biff.models import BiffConfig
from biff.relay import LocalRelay
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from biff.frames import ClientType, Hello
    from biff.relay import Relay


@final
class FakeResolver:
    """Maps a hello to a local relay under ``base / <cwd basename>``.

    Two hellos with the same working directory name share a relay target; the
    user comes from the hello, so one test can seat several users.
    """

    def __init__(self, base: Path) -> None:
        self._base = base

    async def resolve(self, hello: Hello) -> ResolvedSession:
        if Path(hello.cwd).name == "missing":
            msg = f"{hello.cwd} is not a biff repository"
            raise ResolveError(msg)
        return ResolvedSession(
            config=BiffConfig(user=hello.user or "kai", repo_name="test"),
            data_dir=self._base / Path(hello.cwd).name,
            repo_root=None,
        )


@final
class CountingFactory:
    """Builds local relays and counts how many it was asked for."""

    def __init__(self) -> None:
        self.built: list[Path] = []

    def __call__(self, resolved: ResolvedSession) -> Relay:
        self.built.append(resolved.data_dir)
        return LocalRelay(resolved.data_dir)


@dataclass
class Harness:
    """A running daemon plus the clients a test opened against it."""

    daemon: BiffDaemon
    run_dir: RunDir
    factory: CountingFactory
    clients: list[DaemonClient] = field(default_factory=list[DaemonClient])

    async def connect(
        self,
        session_id: str,
        user: str = "kai",
        *,
        main_agent_id: str = "m1",
        cwd: str = "/work/repo",
        client: ClientType = "cli",
    ) -> DaemonClient:
        connected = await DaemonClient.connect(
            run_dir=self.run_dir,
            port=self.daemon.port,
            identity=ClientIdentity(
                client=client,
                session_id=session_id,
                main_agent_id=main_agent_id,
                cwd=cwd,
                user=user,
            ),
        )
        self.clients.append(connected)
        return connected


@pytest.fixture
async def harness(tmp_path: Path) -> AsyncIterator[Harness]:
    run_dir = RunDir(tmp_path / "run")
    factory = CountingFactory()
    daemon = BiffDaemon(
        run_dir=run_dir,
        port=0,
        resolver=FakeResolver(tmp_path / "relays"),
        relay_factory=factory,
        heartbeat_interval=0.05,
        poll_interval=0.02,
    )
    await daemon.start()
    env = Harness(daemon=daemon, run_dir=run_dir, factory=factory)
    yield env
    for connected in env.clients:
        await connected.close()
    await daemon.stop()
