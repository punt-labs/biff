"""One WebSocket connection: authenticate, bind to a session, serve its frames."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self, final

from websockets.exceptions import ConnectionClosed

from biff.daemon.refusal import RefusalError
from biff.daemon.resolver import ResolveError
from biff.daemon.subscription import Subscription
from biff.frame_base import PROTOCOL_VERSION
from biff.frame_client import Hello, Request, Subscribe
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply, ReplyError
from biff.frames import Event, FrameError, Frames
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
        """Prove the token, read the hello; return the attachment.

        Returns ``None`` after closing *ws* when the first frame is not a
        challenge or the next is not a hello whose MAC binds this connection's
        two nonces under the token.  The daemon nonce is made here and lives
        only in this call, so a MAC is good for one connection and one attempt.
        """
        challenge = await self._expect(ws, Challenge, "expected a challenge")
        if challenge is None:
            return None
        daemon_nonce = Handshake.fresh_nonce()
        mac = Handshake.daemon_mac(self._token, challenge.client_nonce, daemon_nonce)
        await self._send(ws, Proof(daemon_nonce=daemon_nonce, mac=mac))
        frame = await self._expect(ws, Hello, "expected a hello")
        if frame is None:
            return None
        expected = Handshake.client_mac(
            self._token, daemon_nonce, challenge.client_nonce
        )
        if not Handshake.matches(frame.mac, expected):
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

    @staticmethod
    async def _expect[F: Challenge | Hello](
        ws: ServerConnection, kind: type[F], why: str
    ) -> F | None:
        """Read one *kind* frame; close *ws* and return ``None`` on anything else."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_HELLO_TIMEOUT_SECONDS)
            frame = Frames.decode(raw if isinstance(raw, str) else raw.decode())
        except (TimeoutError, FrameError, UnicodeDecodeError):
            await ws.close(_POLICY_VIOLATION, why)
            return None
        if not isinstance(frame, kind):
            await ws.close(_POLICY_VIOLATION, why)
            return None
        return frame

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
    async def _send(ws: ServerConnection, frame: Reply | Event | Proof) -> None:
        try:
            await ws.send(Frames.encode(frame))
        except ConnectionClosed:
            logger.debug("client left before the frame was sent", exc_info=True)
