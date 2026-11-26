"""One WebSocket connection: authenticate, bind to a session, serve its frames."""

from __future__ import annotations

import asyncio
import hmac
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from websockets.exceptions import ConnectionClosed

from biff.daemon.refusal import RefusalError
from biff.daemon.resolver import ResolveError
from biff.daemon.subscription import Subscription
from biff.frames import (
    PROTOCOL_VERSION,
    ErrorCode,
    Event,
    FrameError,
    Frames,
    Hello,
    Reply,
    ReplyError,
    Request,
    Subscribe,
)
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from websockets.asyncio.server import ServerConnection

    from biff.daemon.methods import Methods
    from biff.daemon.relays import RelayPool
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.sessions import Attachment, SessionTable

__all__ = ["ConnectionHandler"]

logger = logging.getLogger(__name__)

_POLICY_VIOLATION = 1008
_HELLO_TIMEOUT_SECONDS = 5.0
_HELLO_ID = 0


@dataclass(frozen=True)
class DaemonCore:
    """The state every connection of one daemon shares."""

    resolver: SessionResolver
    pool: RelayPool
    sessions: SessionTable
    methods: Methods
    seq: Iterator[int]
    poll_interval: float


@final
class ConnectionHandler:
    """Serves connections for the daemon's shared state.

    A connection earns no state until its hello names the right token.  A
    frame before the hello, a wrong token, or a second hello closes the
    connection and changes nothing.  Afterwards the connection is bound to the
    one session its hello named; no request can name another.
    """

    __slots__ = ("_core", "_token")

    _token: str
    _core: DaemonCore

    def __new__(cls, token: str, core: DaemonCore) -> Self:
        """Create a handler that admits clients holding *token*."""
        self = super().__new__(cls)
        self._token = token
        self._core = core
        return self

    async def handle(self, ws: ServerConnection) -> None:
        """Serve *ws* until it closes."""
        try:
            attachment = await self._authenticate(ws)
            if attachment is None:
                return
            try:
                await self._serve(ws, attachment)
            finally:
                attachment.detach()
        except ConnectionClosed:
            logger.debug("connection closed by the client", exc_info=True)

    async def _authenticate(self, ws: ServerConnection) -> Attachment | None:
        """Read the hello; return the attachment, or ``None`` after closing *ws*."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_HELLO_TIMEOUT_SECONDS)
            frame = Frames.decode(raw if isinstance(raw, str) else raw.decode())
        except (TimeoutError, FrameError, UnicodeDecodeError):
            await ws.close(_POLICY_VIOLATION, "expected a hello")
            return None
        if not isinstance(frame, Hello) or not hmac.compare_digest(
            frame.token.encode(), self._token.encode()
        ):
            await ws.close(_POLICY_VIOLATION, "unauthorized")
            return None
        try:
            attachment = await self._attach(frame)
        except RefusalError as exc:
            await self._send(ws, Reply(id=_HELLO_ID, error=exc.reply_error))
            await ws.close(_POLICY_VIOLATION, str(exc))
            return None
        await self._send(
            ws,
            Reply(
                id=_HELLO_ID,
                result={
                    "protocol": PROTOCOL_VERSION,
                    "session_key": attachment.session.context.session_key,
                },
            ),
        )
        return attachment

    async def _attach(self, hello: Hello) -> Attachment:
        if hello.version != PROTOCOL_VERSION:
            msg = (
                f"client speaks protocol {hello.version}, biffd speaks "
                f"{PROTOCOL_VERSION}; run: biff daemon restart, or upgrade the client"
            )
            raise RefusalError.of(ErrorCode.UNSUPPORTED_VERSION, msg)
        try:
            resolved = await self._core.resolver.resolve(hello)
        except ResolveError as exc:
            raise RefusalError.of(ErrorCode.BAD_HELLO, str(exc)) from exc
        try:
            relay = self._core.pool.acquire(resolved)
            return await self._core.sessions.open(hello, resolved, relay)
        except RELAY_ERRORS as exc:
            msg = f"cannot reach the relay: {exc}"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc

    async def _serve(self, ws: ServerConnection, attachment: Attachment) -> None:
        async for raw in ws:
            try:
                frame = Frames.decode(raw if isinstance(raw, str) else raw.decode())
            except (FrameError, UnicodeDecodeError):
                await ws.close(_POLICY_VIOLATION, "malformed frame")
                return
            if isinstance(frame, Request):
                await self._send(ws, await self._answer(attachment, frame))
            elif isinstance(frame, Subscribe):
                self._subscribe(ws, attachment)
            else:
                await ws.close(_POLICY_VIOLATION, f"unexpected {frame.type} frame")
                return

    async def _answer(self, attachment: Attachment, request: Request) -> Reply:
        if not attachment.is_current:
            msg = (
                f"session {attachment.session.session_id} has a newer run; "
                "reconnect with a fresh hello"
            )
            error = ReplyError(code=ErrorCode.SESSION_REPLACED, message=msg)
            return Reply(id=request.id, error=error)
        try:
            result = await self._core.methods.call(
                attachment.session.context, request.method, request.params
            )
        except RefusalError as exc:
            return Reply(id=request.id, error=exc.reply_error)
        except RELAY_ERRORS as exc:
            logger.warning("%s failed", request.method, exc_info=True)
            msg = f"{request.method} failed: {exc}"
            return Reply(
                id=request.id, error=ReplyError(code=ErrorCode.INTERNAL, message=msg)
            )
        return Reply(id=request.id, result=result)

    def _subscribe(self, ws: ServerConnection, attachment: Attachment) -> None:
        core = self._core

        async def send(event: Event) -> None:
            await self._send(ws, event)

        subscription = Subscription(
            attachment.session.context, send, core.seq, core.poll_interval
        )
        task = asyncio.create_task(subscription.run())
        if not attachment.hold_subscription(task):
            task.cancel()

    @staticmethod
    async def _send(ws: ServerConnection, frame: Reply | Event) -> None:
        try:
            await ws.send(Frames.encode(frame))
        except ConnectionClosed:
            logger.debug("client left before the frame was sent", exc_info=True)
