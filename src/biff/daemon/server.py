"""The biffd listener: a loopback WebSocket plus an unauthenticated ``/health``."""

from __future__ import annotations

import asyncio
import json
import logging
import signal
import time
from http import HTTPStatus
from importlib.metadata import version as pkg_version
from typing import TYPE_CHECKING, Self, final

from websockets.asyncio.server import Server, ServerConnection, serve

if TYPE_CHECKING:
    from websockets.http11 import Request, Response

    from biff.run_dir import RunDir

__all__ = ["BiffDaemon"]

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
_MAX_FRAME_BYTES = 1_048_576
_POLICY_VIOLATION = 1008


@final
class BiffDaemon:
    """One per-user daemon: owns the listener and its token and port files."""

    __slots__ = ("_port", "_run_dir", "_server", "_started")

    _run_dir: RunDir
    _port: int
    _server: Server | None
    _started: float

    def __new__(cls, *, run_dir: RunDir, port: int) -> Self:
        """Create a daemon that will listen on *port* (0 picks a free one)."""
        self = super().__new__(cls)
        self._run_dir = run_dir
        self._port = port
        self._server = None
        self._started = 0.0
        return self

    @property
    def port(self) -> int:
        """The bound port; the requested one until :meth:`start` has run."""
        return self._port

    async def start(self) -> None:
        """Create the token, bind the loopback listener and record its port."""
        self._run_dir.ensure_token()
        self._server = await serve(
            self._on_connection,
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
        """Stop listening and remove the port file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._run_dir.remove_port()

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

    async def _on_connection(self, connection: ServerConnection) -> None:
        await connection.close(_POLICY_VIOLATION, "no session protocol yet")
