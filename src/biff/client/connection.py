"""A client's connection to biffd: hello, requests, replies and events."""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from biff.frames import (
    ClientType,
    ErrorCode,
    Event,
    FrameError,
    Frames,
    Hello,
    Reply,
    Request,
    Subscribe,
)

if TYPE_CHECKING:
    from pydantic import JsonValue

    from biff.run_dir import RunDir

__all__ = ["ClientIdentity", "ConnectionLostError", "DaemonClient", "DaemonError"]

logger = logging.getLogger(__name__)

_LOOPBACK = "127.0.0.1"


@dataclass(frozen=True)
class ClientIdentity:
    """Who a client is and where it runs: the caller facts of a hello."""

    client: ClientType
    session_id: str
    cwd: str
    main_agent_id: str = ""
    user: str = ""


# Not frozen: raising sets ``__traceback__``, which a frozen dataclass rejects.
@dataclass(eq=False)
class DaemonError(Exception):
    """The daemon refused a frame; ``code`` says why."""

    code: ErrorCode
    message: str

    def __str__(self) -> str:
        """Return the daemon's message."""
        return self.message


class ConnectionLostError(ConnectionError):
    """The daemon closed the connection, or answered with something unreadable."""


@final
class DaemonClient:
    """One connection to the daemon, bound to the session its hello named."""

    __slots__ = ("_events", "_ids", "_pending", "_reader", "_ws")

    _ws: ClientConnection
    _ids: itertools.count[int]
    _pending: dict[int, asyncio.Future[Reply]]
    _events: asyncio.Queue[Event]
    _reader: asyncio.Task[None]

    def __new__(cls, ws: ClientConnection) -> Self:
        """Wrap *ws*, an open connection whose hello the daemon accepted."""
        self = super().__new__(cls)
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending = {}
        self._events = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())
        return self

    @classmethod
    async def connect(
        cls, *, run_dir: RunDir, identity: ClientIdentity, port: int | None = None
    ) -> Self:
        """Open a connection and say hello.

        *port* defaults to the one the running daemon recorded.

        Raises:
            FileNotFoundError: No daemon has created a token.
            ConnectionLostError: The daemon closed the connection, for instance
                because the token was wrong.
            DaemonError: The daemon refused the hello.
        """
        hello = Hello(
            token=run_dir.read_token(),
            client=identity.client,
            session_id=identity.session_id,
            main_agent_id=identity.main_agent_id,
            cwd=identity.cwd,
            user=identity.user,
        )
        bound = run_dir.read_port() if port is None else port
        ws = await connect(f"ws://{_LOOPBACK}:{bound}/")
        try:
            await ws.send(Frames.encode(hello))
            reply = Frames.decode(str(await ws.recv()))
        except (ConnectionClosed, FrameError) as exc:
            await ws.close()
            msg = "biffd closed the connection during hello"
            raise ConnectionLostError(msg) from exc
        if not isinstance(reply, Reply):
            await ws.close()
            msg = f"biffd answered hello with a {reply.type} frame"
            raise ConnectionLostError(msg)
        if reply.error is not None:
            await ws.close()
            raise DaemonError(reply.error.code, reply.error.message)
        return cls(ws)

    @property
    def pending_events(self) -> int:
        """The number of received events no one has read yet."""
        return self._events.qsize()

    async def request(self, method: str, **params: JsonValue) -> JsonValue:
        """Run *method* in the daemon and return its result.

        Raises:
            DaemonError: The daemon refused the request.
            ConnectionLostError: The connection closed before the reply arrived.
        """
        request_id = next(self._ids)
        waiter: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiter
        try:
            await self._ws.send(
                Frames.encode(Request(id=request_id, method=method, params=params))
            )
        except ConnectionClosed as exc:
            self._pending.pop(request_id, None)
            msg = "biffd closed the connection"
            raise ConnectionLostError(msg) from exc
        reply = await waiter
        if reply.error is not None:
            raise DaemonError(reply.error.code, reply.error.message)
        return reply.result

    async def subscribe(self) -> None:
        """Ask for the arrival subscription; read it with :meth:`next_event`."""
        await self._ws.send(Frames.encode(Subscribe()))

    async def next_event(self) -> Event:
        """Return the next event, waiting for one."""
        return await self._events.get()

    async def close(self) -> None:
        """Close the connection; the daemon detaches the session, not ends it."""
        await self._ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def _read(self) -> None:
        """Route replies to their waiters and events to the queue."""
        try:
            async for raw in self._ws:
                frame = Frames.decode(raw if isinstance(raw, str) else raw.decode())
                if isinstance(frame, Reply):
                    waiter = self._pending.pop(frame.id, None)
                    if waiter is not None and not waiter.done():
                        waiter.set_result(frame)
                elif isinstance(frame, Event):
                    self._events.put_nowait(frame)
                else:
                    logger.warning("ignoring a %s frame from biffd", frame.type)
        except (ConnectionClosed, FrameError):
            logger.debug("biffd connection ended", exc_info=True)
        finally:
            lost = ConnectionLostError("biffd closed the connection")
            for waiter in self._pending.values():
                if not waiter.done():
                    waiter.set_exception(lost)
            self._pending.clear()
