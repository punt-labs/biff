"""The biffd listener: a loopback WebSocket plus an unauthenticated ``/health``."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from dataclasses import dataclass
from http import HTTPStatus
from importlib.metadata import version as pkg_version
from itertools import count
from typing import TYPE_CHECKING, Self, final

from websockets.asyncio.server import Server, ServerConnection, serve

from biff.daemon.connection import ConnectionHandler, DaemonCore
from biff.daemon.methods import Methods
from biff.daemon.relays import RelayPool
from biff.daemon.resolver import DiskResolver
from biff.daemon.sessions import SessionTable

if TYPE_CHECKING:
    from websockets.http11 import Request, Response

    from biff.daemon.relays import RelayFactory
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.sessions import SessionStatus
    from biff.run_dir import RunDir

__all__ = ["BiffDaemon", "DaemonStatus"]

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_MAX_FRAME_BYTES = 1_048_576
HEARTBEAT_SECONDS = 60.0
POLL_SECONDS = 1.0
ATTACH_SECONDS = 10.0
REQUEST_SECONDS = 30.0


@dataclass(frozen=True)
class DaemonStatus:
    """What the daemon holds: relay connections and attached sessions."""

    relay_connections: int
    sessions: tuple[SessionStatus, ...]


@final
class BiffDaemon:
    """One per-user daemon: owns the listener, the relays and the sessions."""

    __slots__ = ("_core", "_port", "_run_dir", "_server", "_started")

    _run_dir: RunDir
    _port: int
    _core: DaemonCore
    _server: Server | None
    _started: float

    def __new__(
        cls,
        *,
        run_dir: RunDir,
        port: int,
        resolver: SessionResolver | None = None,
        relay_factory: RelayFactory | None = None,
        heartbeat_interval: float = HEARTBEAT_SECONDS,
        poll_interval: float = POLL_SECONDS,
        attach_timeout: float = ATTACH_SECONDS,
        request_timeout: float = REQUEST_SECONDS,
    ) -> Self:
        """Create a daemon that will listen on *port* (0 picks a free one).

        The resolver and relay factory default to the repository configuration
        on disk and the relay it selects; tests inject their own.
        """
        self = super().__new__(cls)
        self._run_dir = run_dir
        self._port = port
        self._core = DaemonCore(
            resolver=resolver or DiskResolver(),
            pool=RelayPool(relay_factory or RelayPool.selecting()),
            sessions=SessionTable(heartbeat_interval=heartbeat_interval),
            methods=Methods(),
            seq=count(1),
            poll_interval=poll_interval,
            attach_timeout=attach_timeout,
            request_timeout=request_timeout,
        )
        self._server = None
        self._started = 0.0
        return self

    @property
    def port(self) -> int:
        """The bound port; the requested one until :meth:`start` has run."""
        return self._port

    def status(self) -> DaemonStatus:
        """Return the relay connections and sessions the daemon holds."""
        return DaemonStatus(
            relay_connections=self._core.pool.connection_count,
            sessions=self._core.sessions.statuses,
        )

    async def start(self) -> None:
        """Create the token, bind the loopback listener and record its port."""
        handler = ConnectionHandler(self._run_dir.ensure_token(), self._core)
        self._server = await serve(
            handler.handle,
            _LOOPBACK,
            self._port,
            process_request=self._process_request,
            # A browser always sends Origin; no page may reach the daemon.
            origins=[None],
            max_size=_MAX_FRAME_BYTES,
        )
        self._port = self._server.sockets[0].getsockname()[1]
        self._started = time.monotonic()
        self._run_dir.write_port(self._port)
        logger.info("biffd listening on %s:%d", _LOOPBACK, self._port)

    async def stop(self) -> None:
        """Stop listening, close the relays and remove this daemon's port file.

        Each step runs even if an earlier one raised, and the first failure
        still propagates.  The port file is removed only while it still names
        this daemon's port, so a late stop cannot erase its replacement's record.
        """
        server, self._server = self._server, None
        try:
            if server is not None:
                server.close()
                await server.wait_closed()
        finally:
            try:
                await self._core.pool.close()
            finally:
                self._run_dir.remove_port(owned_by=self._port)

    async def run_foreground(self) -> None:
        """Serve until SIGINT or SIGTERM, then shut down cleanly.

        A clean stop removes the port file; a crash leaves it, and the next
        start overwrites it.
        """
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Answer ``/health``; refuse every other path but the WebSocket root."""
        if request.path == "/health":
            body = {
                "version": pkg_version("punt-biff"),
                "uptime_seconds": round(time.monotonic() - self._started, 3),
            }
            response = connection.respond(HTTPStatus.OK, json.dumps(body))
            # respond() stamps text/plain; setting would add a second header.
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = "application/json"
            return response
        if request.path != "/":
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        return None
