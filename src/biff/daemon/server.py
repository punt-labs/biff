"""The biffd listener: a loopback WebSocket plus an unauthenticated ``/health``."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from dataclasses import dataclass
from http import HTTPStatus
from importlib.metadata import version as pkg_version
from itertools import count
from typing import TYPE_CHECKING, Self, final

from websockets.asyncio.server import Server, ServerConnection, serve

from biff.daemon.connection import ConnectionHandler, DaemonCore
from biff.daemon.handback import Handback
from biff.daemon.harness_records import HarnessRecords
from biff.daemon.methods import READ_BUDGET_BYTES, Methods
from biff.daemon.relays import RelayPool
from biff.daemon.resolver import DiskResolver
from biff.daemon.session_methods import SessionMethods
from biff.daemon.session_restore import SessionRestore
from biff.daemon.session_sweep import SessionSweeper
from biff.daemon.sessions import SessionTable
from biff.daemon.spill import Spill
from biff.daemon.tasks import Supervised
from biff.frame_base import MAX_FRAME_BYTES

if TYPE_CHECKING:
    from websockets.http11 import Request, Response

    from biff.daemon.relays import RelayFactory
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.session_store import CliSessionStore
    from biff.daemon.sessions import SessionStatus
    from biff.run_dir import RunDir

__all__ = ["BiffDaemon", "DaemonStatus"]

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"
HEARTBEAT_SECONDS = 60.0
POLL_SECONDS = 1.0
ATTACH_SECONDS = 10.0
REQUEST_SECONDS = 30.0
ORG_REFRESH_SECONDS = 600.0
SESSION_SWEEP_SECONDS = 600.0


@dataclass(frozen=True)
class DaemonStatus:
    """What the daemon holds: relay connections and attached sessions."""

    relay_connections: int
    sessions: tuple[SessionStatus, ...]
    spilled: int
    undeliverable: int
    set_aside: int


@final
class BiffDaemon:
    """One per-user daemon: owns the listener, the relays and the sessions."""

    __slots__ = (
        "_core",
        "_port",
        "_restore",
        "_run_dir",
        "_server",
        "_spill",
        "_spill_task",
        "_started",
        "_sweep_interval",
        "_sweep_task",
        "_sweeper",
    )

    _run_dir: RunDir
    _port: int
    _core: DaemonCore
    _restore: SessionRestore
    _spill: Spill
    _spill_task: asyncio.Task[None] | None
    _server: Server | None
    _started: float
    _sweeper: SessionSweeper
    _sweep_interval: float
    _sweep_task: asyncio.Task[None] | None

    def __new__(
        cls,
        *,
        run_dir: RunDir,
        port: int,
        sessions: CliSessionStore,
        resolver: SessionResolver | None = None,
        relay_factory: RelayFactory | None = None,
        heartbeat_interval: float = HEARTBEAT_SECONDS,
        poll_interval: float = POLL_SECONDS,
        attach_timeout: float = ATTACH_SECONDS,
        request_timeout: float = REQUEST_SECONDS,
        read_budget: int = READ_BUDGET_BYTES,
        org_refresh_interval: float = ORG_REFRESH_SECONDS,
        session_sweep_interval: float = SESSION_SWEEP_SECONDS,
    ) -> Self:
        """Create a daemon that will listen on *port* (0 picks a free one).

        *sessions* holds the CLI session records the daemon owns; it sweeps them
        once at start and every *session_sweep_interval* seconds after.

        The resolver and relay factory default to the repository configuration
        on disk and the relay it selects; tests inject their own.
        """
        self = super().__new__(cls)
        self._run_dir = run_dir
        self._port = port
        resolver = resolver or DiskResolver()
        pool = RelayPool(relay_factory or RelayPool.selecting())
        records = HarnessRecords(sessions.root.parent / "harness-sessions")
        spill = Spill(
            sessions.root.parent / "handback-spill.json",
            run_dir.path / "handback-spill.json",
        )
        live = SessionTable(
            heartbeat_interval=heartbeat_interval,
            org_refresh_interval=org_refresh_interval,
            records=records,
        )
        self._core = DaemonCore(
            resolver=resolver,
            pool=pool,
            sessions=live,
            methods=Methods(
                SessionMethods(sessions, live),
                read_budget,
                annotate_status=spill.annotate,
            ),
            seq=count(1),
            poll_interval=poll_interval,
            attach_timeout=attach_timeout,
            request_timeout=request_timeout,
        )
        self._restore = SessionRestore(records, self._core)
        self._spill = spill
        self._spill_task = None
        self._server = None
        self._started = 0.0
        self._sweeper = SessionSweeper(sessions, resolver, pool, live)
        self._sweep_interval = session_sweep_interval
        self._sweep_task = None
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
            spilled=self._spill.pending(),
            undeliverable=self._spill.undeliverable(),
            set_aside=self._spill.set_aside(),
        )

    async def start(self) -> None:
        """Create the token, bind the loopback listener and record its port."""
        await self._spill.resume(self._core)  # mail the last stop could not file
        self._spill_task = Supervised.spawn(
            self._spill.keep_filing(self._core), "spill retry"
        )
        await self._restore.run()
        handler = ConnectionHandler(self._run_dir.ensure_token(), self._core)
        self._server = await serve(
            handler.handle,
            _LOOPBACK,
            self._port,
            process_request=self._process_request,
            # A browser always sends Origin; no page may reach the daemon.
            origins=[None],
            max_size=MAX_FRAME_BYTES,
        )
        self._port = self._server.sockets[0].getsockname()[1]
        self._started = time.monotonic()
        self._run_dir.write_port(self._port)
        self._sweep_task = Supervised.spawn(self._sweep_forever(), "session sweep")
        logger.info("biffd listening on %s:%d", _LOOPBACK, self._port)

    async def sweep_sessions(self) -> None:
        """Lapse stale stored sessions and expire those past retention, once."""
        await self._sweeper.sweep()

    async def stop(self) -> None:
        """Stop listening, close the relays and remove this daemon's port file.

        Each step runs even if an earlier one raised, and the first failure
        still propagates.  The port file is removed only while it still names
        this daemon's port, so a late stop cannot erase its replacement's record.
        """
        await self._restore.stop()
        server, self._server = self._server, None
        sweep, self._sweep_task = self._sweep_task, None
        refiling, self._spill_task = self._spill_task, None
        for task in (sweep, refiling):
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        try:
            if server is not None:
                server.close()
                await self._wait_closed(server)
        finally:
            try:
                await Handback.close_relays(self._core, self._spill)
            finally:
                self._run_dir.remove_port(owned_by=self._port)

    @staticmethod
    async def _wait_closed(server: Server) -> None:
        """Wait for the listener's connections to close, for a bounded time.

        A request in flight can hold the listener for its own timeout; the
        hand-back has the mail to give back and the supervisor's deadline to meet.
        """
        try:
            async with asyncio.timeout(Handback.CLOSE_SECONDS):
                await server.wait_closed()
        except TimeoutError:
            logger.warning(
                "the listener did not close within %g s; stopping anyway",
                Handback.CLOSE_SECONDS,
            )

    async def run_foreground(self) -> None:
        """Serve until SIGINT or SIGTERM, then shut down cleanly.

        A clean stop removes the port file; a crash leaves it, and the next
        start overwrites it.
        """
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop, sig, stop)
        await self.start()
        try:
            await stop.wait()
        finally:
            await self.stop()

    @staticmethod
    def _request_stop(sig: signal.Signals, stop: asyncio.Event) -> None:
        """Say which signal asked biffd to stop, then let it shut down."""
        logger.info("biffd stopping: signal=%s pid=%d", sig.name, os.getpid())
        stop.set()

    async def _sweep_forever(self) -> None:
        """Sweep at start, then every interval, for as long as the daemon runs."""
        while True:
            await self._sweeper.sweep()
            await asyncio.sleep(self._sweep_interval)

    def _process_request(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        """Answer ``/health``; refuse every other path but the WebSocket root."""
        if request.path == "/health":
            body = {
                "version": pkg_version("punt-biff"),
                "uptime_seconds": round(time.monotonic() - self._started, 3),
                "pid": os.getpid(),
            }
            response = connection.respond(HTTPStatus.OK, json.dumps(body))
            # respond() stamps text/plain; setting would add a second header.
            del response.headers["Content-Type"]
            response.headers["Content-Type"] = "application/json"
            return response
        if request.path != "/":
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        return None
