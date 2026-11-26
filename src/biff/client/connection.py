"""A client's connection to biffd: hello, requests, replies and events."""

from __future__ import annotations

import asyncio
import itertools
import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Self, final

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from biff.client.environment import ClientEnvironment
from biff.frame_base import PROTOCOL_VERSION
from biff.frame_client import ClientType, Hello, Request, Subscribe
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply
from biff.frames import Event, FrameError, Frames

if TYPE_CHECKING:
    from collections.abc import Callable

    from pydantic import JsonValue

    from biff.run_dir import RunDir

__all__ = [
    "ClientIdentity",
    "ConnectionLostError",
    "DaemonClient",
    "DaemonError",
    "DaemonUnreachableError",
    "RequestTimeoutError",
    "UnprovenDaemonError",
]

logger = logging.getLogger(__name__)

_LOOPBACK: Final = "127.0.0.1"
_OPEN_TIMEOUT_SECONDS: Final = 1.0
_HANDSHAKE_TIMEOUT_SECONDS: Final = 5.0
_REQUEST_TIMEOUT_SECONDS: Final = 10.0
_VERSION_FIX: Final = (
    "stop the running `biff daemon serve` and start it again, or upgrade biff"
)


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


class UnprovenDaemonError(ConnectionError):
    """The listener did not prove it holds the biffd token; no credential was sent."""


class DaemonUnreachableError(ConnectionError):
    """Nothing answers where biffd should be, or its port record is unusable."""


class ConnectionLostError(ConnectionError):
    """The daemon closed the connection, or answered with something unreadable."""


class RequestTimeoutError(TimeoutError):
    """The daemon did not answer a request in time."""


@final
class DaemonClient:
    """One connection to the daemon, bound to the session its hello named."""

    __slots__ = (
        "_events",
        "_ids",
        "_lost",
        "_pending",
        "_reader",
        "_request_timeout",
        "_ws",
    )

    _ws: ClientConnection
    _ids: itertools.count[int]
    _pending: dict[int, asyncio.Future[Reply]]
    _events: asyncio.Queue[Event | None]
    _reader: asyncio.Task[None]
    _lost: str
    _request_timeout: float

    def __new__(cls, ws: ClientConnection, request_timeout: float) -> Self:
        """Wrap *ws*, an open connection whose hello the daemon accepted."""
        self = super().__new__(cls)
        self._ws = ws
        self._ids = itertools.count(1)
        self._pending = {}
        self._events = asyncio.Queue()
        self._lost = ""
        self._request_timeout = request_timeout
        self._reader = asyncio.create_task(self._read())
        return self

    @classmethod
    async def connect(
        cls,
        *,
        run_dir: RunDir,
        identity: ClientIdentity,
        port: int | None = None,
        proof_timeout: float = _HANDSHAKE_TIMEOUT_SECONDS,
        request_timeout: float = _REQUEST_TIMEOUT_SECONDS,
    ) -> Self:
        """Open a connection, check the daemon's proof of the token, say hello.

        *port* defaults to the one the running daemon recorded, or 8424.  The
        token never crosses the wire.  The hello carries a MAC bound to both
        handshake nonces, sent only after the listener proves it holds the token,
        so a process that merely bound the port gets nothing it can reuse.
        *proof_timeout* bounds the proof and the hello's reply;
        *request_timeout* bounds each later request.

        Raises:
            UnsupportedOverrideError: The environment sets a ``BIFF_RELAY_*``
                variable biffd cannot carry.
            FileNotFoundError: No daemon has created a token.
            PermissionError: The run directory or token file is not private.
            ValueError: The token file does not hold a token.
            DaemonUnreachableError: Nothing answers on the port, or the port
                file is corrupt.
            UnprovenDaemonError: The listener did not prove it holds the token.
            ConnectionLostError: The daemon closed the connection, or did not
                answer the hello in time.
            DaemonError: The daemon refused the hello, or speaks another
                protocol version.
        """
        ClientEnvironment.require_no_relay_overrides(os.environ)
        token = run_dir.read_token()
        bound = cls._port(run_dir, port)
        ws = await cls._open(bound)
        mac = await cls._handshake(ws, token, bound, proof_timeout)
        hello = Hello(
            mac=mac,
            client=identity.client,
            session_id=identity.session_id,
            main_agent_id=identity.main_agent_id,
            cwd=identity.cwd,
            user=ClientEnvironment.user(identity.user, os.environ),
        )
        try:
            await ws.send(Frames.encode(hello))
            frame = cls._decode(
                await asyncio.wait_for(ws.recv(), timeout=proof_timeout)
            )
        except TimeoutError as exc:
            await ws.close()
            msg = (
                f"biffd did not answer the hello within {proof_timeout:g}s; check "
                f"it with: curl -s http://{_LOOPBACK}:{bound}/health"
            )
            raise ConnectionLostError(msg) from exc
        except ConnectionClosed as exc:
            await ws.close()
            raise ConnectionLostError(cls._why(exc)) from exc
        except (FrameError, UnicodeDecodeError) as exc:
            await ws.close()
            msg = "biffd sent an unreadable reply to the hello"
            raise ConnectionLostError(msg) from exc
        except DaemonError:
            await ws.close()
            raise
        if not isinstance(frame, Reply):
            await ws.close()
            msg = f"biffd answered hello with a {frame.type} frame"
            raise ConnectionLostError(msg)
        if frame.error is not None:
            await ws.close()
            raise DaemonError(frame.error.code, frame.error.message)
        return cls(ws, request_timeout)

    @staticmethod
    def _port(run_dir: RunDir, port: int | None) -> int:
        """Return the port to dial: *port*, else the daemon's recorded one."""
        if port is not None:
            return port
        try:
            return run_dir.read_port()
        except ValueError as exc:
            msg = f"{exc}; remove it and start biffd with: biff daemon serve"
            raise DaemonUnreachableError(msg) from exc

    @staticmethod
    async def _open(port: int) -> ClientConnection:
        """Open the WebSocket, telling a missing daemon from a wrong listener."""
        try:
            # proxy=None: the daemon is on this machine, never behind a proxy that
            # the environment's HTTP_PROXY or WS_PROXY would otherwise name.
            return await connect(
                f"ws://{_LOOPBACK}:{port}/",
                open_timeout=_OPEN_TIMEOUT_SECONDS,
                proxy=None,
            )
        except InvalidHandshake as exc:
            msg = (
                f"the listener on {_LOOPBACK}:{port} is not biffd (it did not "
                "complete a WebSocket handshake); no credential was sent"
            )
            raise UnprovenDaemonError(msg) from exc
        except (OSError, TimeoutError) as exc:
            msg = (
                f"biffd is not answering on {_LOOPBACK}:{port}; start it with: "
                "biff daemon serve"
            )
            raise DaemonUnreachableError(msg) from exc

    @staticmethod
    def _decode(
        raw: str | bytes,
    ) -> Challenge | Proof | Hello | Request | Reply | Subscribe | Event:
        """Decode a frame from the daemon, naming a protocol version mismatch.

        Raises:
            DaemonError: The daemon speaks another protocol version, whatever
                shape its frame has.
            FrameError: The frame is not valid.
            UnicodeDecodeError: A binary frame is not UTF-8.
        """
        text = raw if isinstance(raw, str) else raw.decode()
        version = Frames.version_of(text)
        if version is not None and version != PROTOCOL_VERSION:
            msg = (
                f"biffd speaks protocol {version}, this client speaks "
                f"{PROTOCOL_VERSION}; {_VERSION_FIX}"
            )
            raise DaemonError(ErrorCode.UNSUPPORTED_VERSION, msg)
        return Frames.decode(text)

    @staticmethod
    def _why(exc: ConnectionClosed) -> str:
        """Describe a close with its code and reason."""
        if exc.rcvd is None:
            return "biffd closed the connection without a close frame"
        return DaemonClient._closed_text(exc.rcvd.code, exc.rcvd.reason)

    @staticmethod
    def _closed_text(code: int | None, reason: str | None) -> str:
        return (
            f"biffd closed the connection (code {code}: {reason or 'no reason given'})"
        )

    @staticmethod
    async def _handshake(
        ws: ClientConnection, token: str, port: int, timeout: float
    ) -> str:
        """Verify the daemon's proof of *token*; return the MAC for the hello.

        Closes *ws* and raises unless the peer proves it holds *token*.  Only a
        MAC bound to this connection's two nonces leaves the process.
        """
        challenge = Challenge.fresh()
        failure = (
            f"the listener on {_LOOPBACK}:{port} did not prove it is biffd; no "
            "credential was sent. See what owns the port with: "
            f"lsof -nP -iTCP:{port} -sTCP:LISTEN"
        )
        try:
            await ws.send(Frames.encode(challenge))
            proof = DaemonClient._decode(
                await asyncio.wait_for(ws.recv(), timeout=timeout)
            )
        except (TimeoutError, ConnectionClosed, FrameError, UnicodeDecodeError) as exc:
            await ws.close()
            raise UnprovenDaemonError(failure) from exc
        except DaemonError:
            await ws.close()
            raise
        if isinstance(proof, Reply) and proof.error is not None:
            await ws.close()
            raise DaemonError(proof.error.code, proof.error.message)
        if not isinstance(proof, Proof) or not proof.valid_for(challenge, token):
            await ws.close()
            raise UnprovenDaemonError(failure)
        return Handshake.client_mac(token, proof.daemon_nonce, challenge.client_nonce)

    @property
    def pending_events(self) -> int:
        """The number of received events no one has read yet."""
        return self._events.qsize() - (1 if self._lost else 0)

    async def request(self, method: str, **params: JsonValue) -> JsonValue:
        """Run *method* in the daemon and return its result.

        Raises:
            DaemonError: The daemon refused the request.
            RequestTimeoutError: No reply arrived within the request timeout.
            ConnectionLostError: The connection closed before the reply arrived.
        """
        reply = await self._roundtrip(
            lambda i: Request(id=i, method=method, params=params), method
        )
        if reply.error is not None:
            raise DaemonError(reply.error.code, reply.error.message)
        return reply.result

    async def subscribe(self) -> None:
        """Ask for the arrival subscription; read it with :meth:`next_event`.

        Raises:
            DaemonError: The daemon refused: this connection is already
                subscribed, or its run was replaced.
            RequestTimeoutError: The daemon did not answer in time.
            ConnectionLostError: The connection closed.
        """
        reply = await self._roundtrip(lambda i: Subscribe(id=i), "subscribe")
        if reply.error is not None:
            raise DaemonError(reply.error.code, reply.error.message)

    async def next_event(self) -> Event:
        """Return the next event, waiting for one.

        Raises:
            ConnectionLostError: The connection closed and no event is left.
        """
        item = await self._events.get()
        if item is None:
            self._events.put_nowait(None)  # wake every later caller too
            raise ConnectionLostError(self._lost)
        return item

    async def close(self) -> None:
        """Close the connection; the daemon detaches the session, not ends it."""
        await self._ws.close()
        await asyncio.gather(self._reader, return_exceptions=True)

    async def _roundtrip(
        self, build: Callable[[int], Request | Subscribe], what: str
    ) -> Reply:
        """Send the frame *build* makes and wait for its reply."""
        self._require_open()
        request_id = next(self._ids)
        waiter: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = waiter
        try:
            try:
                await self._ws.send(Frames.encode(build(request_id)))
            except ConnectionClosed as exc:
                raise ConnectionLostError(self._why(exc)) from exc
            # The reader may have stopped while the frame was sent, after it
            # failed the waiters that existed then.
            self._require_open()
            return await asyncio.wait_for(waiter, timeout=self._request_timeout)
        except TimeoutError:
            msg = (
                f"{what} got no reply from biffd within {self._request_timeout:g}s; "
                "it may be stuck on a slow relay (see ~/.punt-labs/biff/logs/biffd.log)"
            )
            raise RequestTimeoutError(msg) from None
        finally:
            self._pending.pop(request_id, None)

    def _require_open(self) -> None:
        if self._lost:
            raise ConnectionLostError(self._lost)

    async def _read(self) -> None:
        """Route replies to their waiters and events to the queue."""
        reason = ""
        try:
            async for raw in self._ws:
                frame = Frames.decode(raw if isinstance(raw, str) else raw.decode())
                if isinstance(frame, Reply):
                    waiter = self._pending.pop(frame.id, None)
                    if waiter is None:
                        logger.warning(
                            "reply for unknown request id %d (answered after it "
                            "was abandoned, or never asked)",
                            frame.id,
                        )
                    elif not waiter.done():
                        waiter.set_result(frame)
                elif isinstance(frame, Event):
                    self._events.put_nowait(frame)
                else:
                    logger.warning("ignoring a %s frame from biffd", frame.type)
            reason = self._closed_text(self._ws.close_code, self._ws.close_reason)
        except ConnectionClosed as exc:
            reason = self._why(exc)
        except (FrameError, UnicodeDecodeError) as exc:
            reason = f"biffd sent a frame this client cannot read ({exc})"
            logger.warning("%s", reason)
        finally:
            await self._lose(reason or "biffd closed the connection")

    async def _lose(self, reason: str) -> None:
        """Record the loss, fail what waits, and close the socket."""
        self._lost = reason
        lost = ConnectionLostError(reason)
        for waiter in self._pending.values():
            if not waiter.done():
                waiter.set_exception(lost)
        self._pending.clear()
        self._events.put_nowait(None)
        await asyncio.shield(self._ws.close())
