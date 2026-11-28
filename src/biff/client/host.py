"""Connect to this host's biffd, starting it first when it is down."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Self, final

from biff.client.connection import DaemonClient
from biff.client.nudge import DaemonNudge
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from biff.client.connection import ClientIdentity

__all__ = ["HostConnector"]


@final
class HostConnector:
    """Opens a connection to the biffd of one run directory."""

    __slots__ = ("_run_dir",)

    _run_dir: RunDir

    def __new__(cls, run_dir: RunDir) -> Self:
        """Connect through the daemon whose files are in *run_dir*."""
        self = super().__new__(cls)
        self._run_dir = run_dir
        return self

    @classmethod
    def for_host(cls) -> Self:
        """Return the connector for this user's default run directory."""
        return cls(RunDir.default())

    async def connect(self, identity: ClientIdentity) -> DaemonClient:
        """Start biffd if it is down, then say hello as *identity*."""
        # Blocking: it can wait on the service manager, so it leaves the loop.
        port = await asyncio.to_thread(DaemonNudge.for_host(self._run_dir).ensure_running)
        return await DaemonClient.connect(
            run_dir=self._run_dir, port=port, identity=identity
        )
