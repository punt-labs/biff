"""Connect to this host's biffd, starting it first when it is down."""

from __future__ import annotations

import asyncio
import logging
import threading
from concurrent.futures import Future
from typing import TYPE_CHECKING, Self, final

from biff.client.connection import DaemonClient
from biff.client.nudge import DaemonNudge
from biff.run_dir import RunDir

if TYPE_CHECKING:
    from biff.client.connection import ClientIdentity

__all__ = ["HostConnector"]

logger = logging.getLogger(__name__)


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
        """Start biffd if it is down, then say hello as *identity*.

        Cancelling the call, a deadline included, returns at once.
        """
        port = await asyncio.wrap_future(self._discover())
        return await DaemonClient.connect(
            run_dir=self._run_dir, port=port, identity=identity
        )

    def _discover(self) -> Future[int]:
        """Find or start biffd on a thread that never holds up the process's exit.

        The service manager can be slow, and a thread cannot be cancelled.  A
        pool thread is joined when the interpreter exits, so a hook past its
        deadline would still wait for it; a daemon thread is simply left behind.
        """
        found: Future[int] = Future()

        def work() -> None:
            if not found.set_running_or_notify_cancel():
                return
            try:
                port = DaemonNudge.for_host(self._run_dir).ensure_running()
            except Exception as exc:
                logger.warning("could not reach or start biffd", exc_info=True)
                found.set_exception(exc)
            else:
                found.set_result(port)

        threading.Thread(target=work, name="biffd-discovery", daemon=True).start()
        return found
