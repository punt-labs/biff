"""One WebSocket connection: authenticate, bind to a session, serve its frames."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final, Self, final

from websockets.exceptions import ConnectionClosed

from biff.daemon.acks import Acknowledgements
from biff.daemon.refusal import ClientGoneError, HandshakeRefusedError, RefusalError
from biff.daemon.resolver import ResolveError
from biff.daemon.subscription import Subscription
from biff.daemon.tasks import Supervised
from biff.frame_ack import Ack
from biff.frame_base import MAX_FRAME_BYTES, PROTOCOL_VERSION
from biff.frame_client import Hello, Request, Subscribe
from biff.frame_proof import Challenge, Handshake, Proof
from biff.frame_reply import ErrorCode, Reply, ReplyError
from biff.frames import Event, FrameError, Frames
from biff.log_hint import LOG_HINT
from biff.relay_errors import RELAY_ERRORS

if TYPE_CHECKING:
    from collections.abc import Iterator

    from pydantic import JsonValue
    from websockets.asyncio.server import ServerConnection

    from biff.daemon.attachment import Attachment
    from biff.daemon.methods import Acknowledge, Methods
    from biff.daemon.relays import RelayPool
    from biff.daemon.resolver import SessionResolver
    from biff.daemon.sessions import SessionTable

__all__ = ["ConnectionHandler", "DaemonCore"]

logger = logging.getLogger(__name__)

_POLICY_VIOLATION = 1008
_INTERNAL_ERROR = 1011
_HELLO_TIMEOUT_SECONDS: Final = 5.0
_HELLO_ID: Final = 0
_MAX_CLOSE_REASON_BYTES: Final = 120
_REPLACED_REASON: Final = "session replaced by a newer run; reconnect"
_ENDED_REASON: Final = "session ended"


@dataclass(frozen=True)
class DaemonCore:
    """The state every connection of one daemon shares."""

    resolver: SessionResolver
    pool: RelayPool
    sessions: SessionTable
    methods: Methods
    seq: Iterator[int]
    poll_interval: float
    attach_timeout: float
    request_timeout: float


@final
class ConnectionHandler:
    """Serves connections for the daemon's shared state.

    A connection earns no state until it passes the handshake: a challenge, the
    daemon's proof of the token, and a hello whose MAC binds both nonces.  A
    frame out of order, a wrong MAC, a second hello or a silent client closes the
    connection, logs why and changes nothing.  Afterwards the connection is bound
    to the one session its hello named; no request can name another.
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
        peer = ws.remote_address
        try:
            attachment = await self._authenticate(ws)
        except HandshakeRefusedError as exc:
            logger.warning("refused a connection from %s: %s", peer, exc.reason)
            await self._close(ws, _POLICY_VIOLATION, exc.reason)
            return
        attachment.on_replaced(partial(self._end_replaced, ws))
        attachment.on_ended(partial(self._end_ended, ws))
        try:
            await self._serve(ws, attachment)
        except ConnectionClosed:
            logger.debug("connection closed by the client", exc_info=True)
        finally:
            self._core.sessions.release(attachment)

    async def _authenticate(self, ws: ServerConnection) -> Attachment:
        """Prove the token, read the hello; return the attachment.

        The daemon nonce is made here and lives only in this call, so a MAC is
        good for one connection and one attempt.

        Raises:
            HandshakeRefusedError: The client sent something other than a
                challenge and then a hello, sent a MAC that does not bind this
                connection's nonces, spoke another protocol version, or was
                refused by the session table.
        """
        challenge = await self._read(ws, Challenge, "expected a challenge")
        daemon_nonce = Handshake.fresh_nonce()
        mac = Handshake.daemon_mac(self._token, challenge.client_nonce, daemon_nonce)
        try:
            await ws.send(Frames.encode(Proof(daemon_nonce=daemon_nonce, mac=mac)))
        except ConnectionClosed as exc:
            msg = "client left before the proof was sent"
            raise HandshakeRefusedError(msg) from exc
        hello = await self._read(ws, Hello, "expected a hello")
        expected = Handshake.client_mac(
            self._token, daemon_nonce, challenge.client_nonce
        )
        if not Handshake.matches(hello.mac, expected):
            msg = "hello MAC did not match this connection's nonces"
            raise HandshakeRefusedError(msg)
        try:
            attachment = await self._attach(hello)
        except RefusalError as exc:
            await self._send(ws, Reply(id=_HELLO_ID, error=exc.reply_error))
            raise HandshakeRefusedError(str(exc)) from exc
        result: dict[str, JsonValue] = {
            "protocol": PROTOCOL_VERSION,
            "session_key": attachment.session.context.session_key,
        }
        await self._send(ws, Reply(id=_HELLO_ID, result=result))
        return attachment

    async def _read[F: Challenge | Hello](
        self, ws: ServerConnection, kind: type[F], why: str
    ) -> F:
        """Read one *kind* frame, checking its protocol version first.

        The version is read from the raw object before the frame is validated, so
        a client of another version, whose frames may have another shape, is told
        which versions are involved instead of being closed without a word.

        Raises:
            HandshakeRefusedError: The frame is late, malformed, not a *kind*,
                or of another protocol version.
        """
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=_HELLO_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            msg = f"{why}: nothing arrived within {_HELLO_TIMEOUT_SECONDS:g}s"
            raise HandshakeRefusedError(msg) from exc
        except ConnectionClosed as exc:
            msg = f"client left while biffd waited for it ({why})"
            raise HandshakeRefusedError(msg) from exc
        try:
            text = raw if isinstance(raw, str) else raw.decode()
        except UnicodeDecodeError as exc:
            raise HandshakeRefusedError(why) from exc
        version = Frames.version_of(text)
        if version is not None and version != PROTOCOL_VERSION:
            refusal = self._version_refusal(version)
            await self._send(ws, Reply(id=_HELLO_ID, error=refusal.reply_error))
            raise HandshakeRefusedError(f"client speaks protocol {version}")
        try:
            frame = Frames.decode(text)
        except FrameError as exc:
            raise HandshakeRefusedError(why) from exc
        if not isinstance(frame, kind):
            raise HandshakeRefusedError(why)
        return frame

    @staticmethod
    def _version_refusal(version: int) -> RefusalError:
        msg = (
            f"client speaks protocol {version}, biffd speaks {PROTOCOL_VERSION}; "
            "stop the running `biff daemon serve` and start it again, or upgrade "
            "the client"
        )
        return RefusalError.of(ErrorCode.UNSUPPORTED_VERSION, msg)

    async def _attach(self, hello: Hello) -> Attachment:
        """Resolve the hello's session and attach to it.

        Raises:
            RefusalError: The directory has no biff configuration, the relay is
                unreachable or too slow, the session id belongs to another user,
                or biffd failed in a way its log explains.
        """
        core = self._core
        try:
            async with asyncio.timeout(core.attach_timeout):
                resolved = await core.resolver.resolve(hello)
                return await core.sessions.open(hello, resolved, core.pool)
        except RefusalError:
            raise
        except ResolveError as exc:
            raise RefusalError.of(ErrorCode.BAD_HELLO, str(exc)) from exc
        except TimeoutError as exc:
            msg = f"relay did not answer within {core.attach_timeout:g}s"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc
        except RELAY_ERRORS as exc:
            msg = f"cannot reach the relay: {exc}"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc
        except Exception as exc:
            logger.exception("hello for session %s failed", hello.session_id)
            msg = f"hello failed in biffd: {exc}; {LOG_HINT}"
            raise RefusalError.of(ErrorCode.INTERNAL, msg) from exc

    async def _serve(self, ws: ServerConnection, attachment: Attachment) -> None:
        """Answer frames until the connection ends; requests run concurrently.

        A reply carries its request's id, so replies may leave in any order.
        """
        running: set[asyncio.Task[None]] = set()
        acks = Acknowledgements()
        try:
            async for raw in ws:
                try:
                    text = raw if isinstance(raw, str) else raw.decode()
                    frame = Frames.decode(text)
                except (FrameError, UnicodeDecodeError):
                    await self._close(ws, _POLICY_VIOLATION, "malformed frame")
                    return
                if isinstance(frame, Request):
                    task = Supervised.spawn(
                        self._reply(ws, attachment, frame, acks),
                        f"request {frame.method}",
                    )
                    running.add(task)
                    task.add_done_callback(running.discard)
                elif isinstance(frame, Ack):
                    # In order with the requests: a read sent after the ack must
                    # not fetch mail the ack is about to mark read.
                    await self._confirm(acks, frame)
                elif isinstance(frame, Subscribe):
                    await self._send(ws, self._subscribe(ws, attachment, frame))
                else:
                    reason = f"unexpected {frame.type} frame"
                    await self._close(ws, _POLICY_VIOLATION, reason)
                    return
        finally:
            for task in running:
                task.cancel()
            if acks:
                logger.info(
                    "connection ended with %d replies unconfirmed; their mail "
                    "stays unread",
                    len(acks),
                )

    async def _confirm(self, acks: Acknowledgements, ack: Ack) -> None:
        """Run what the client's receipt of reply *ack* releases; log a failure."""
        try:
            async with asyncio.timeout(self._core.request_timeout):
                await acks.confirm(ack.id)
        except Exception:
            logger.exception("acknowledging request %d failed", ack.id)

    async def _reply(
        self,
        ws: ServerConnection,
        attachment: Attachment,
        request: Request,
        acks: Acknowledgements,
    ) -> None:
        """Answer *request*; what the answer carries is acknowledged on the ack.

        Mail marked read once the reply was written would be lost with a reply
        the client gave up on, so acknowledgement waits for the client's ack.
        """
        reply, acknowledge = await self._answer(attachment, request)
        if (size := len(Frames.encode(reply).encode())) > MAX_FRAME_BYTES:
            msg = (
                f"{request.method} reply of {size} bytes exceeds the "
                f"{MAX_FRAME_BYTES}-byte frame limit; nothing was marked read"
            )
            logger.error("%s", msg)
            reply, acknowledge = (
                self._failure(request.id, ErrorCode.INTERNAL, msg),
                None,
            )
        if acknowledge is not None:
            acks.expect(request.id, acknowledge)  # before the send: the ack races it
        if not await self._send(ws, reply):
            acks.forget(request.id)

    async def _answer(
        self, attachment: Attachment, request: Request
    ) -> tuple[Reply, Acknowledge | None]:
        """Run one request; no failure escapes this method.

        Returns the reply and the acknowledgement to run once it was sent.
        """
        method, core = request.method, self._core
        if not attachment.is_current:
            replaced = self._failure(
                request.id, ErrorCode.SESSION_REPLACED, _REPLACED_REASON
            )
            return replaced, None
        try:
            async with asyncio.timeout(core.request_timeout):
                outcome = await core.methods.call(
                    attachment.session, method, request.params
                )
        except RefusalError as exc:
            return Reply(id=request.id, error=exc.reply_error), None
        except TimeoutError:
            msg = f"{method} timed out after {core.request_timeout:g}s"
            return self._failure(request.id, ErrorCode.INTERNAL, msg), None
        except RELAY_ERRORS as exc:
            logger.warning("%s failed", method, exc_info=True)
            msg = f"{method} failed: {exc}"
            return self._failure(request.id, ErrorCode.INTERNAL, msg), None
        except Exception as exc:
            logger.exception("%s failed in biffd", method)
            msg = f"{method} failed in biffd: {exc}; {LOG_HINT}"
            return self._failure(request.id, ErrorCode.INTERNAL, msg), None
        reply = Reply(
            id=request.id,
            result=outcome.result,
            needs_ack=outcome.acknowledge is not None,
        )
        return reply, outcome.acknowledge

    @staticmethod
    def _failure(request_id: int, code: ErrorCode, message: str) -> Reply:
        return Reply(id=request_id, error=ReplyError(code=code, message=message))

    def _subscribe(
        self, ws: ServerConnection, attachment: Attachment, request: Subscribe
    ) -> Reply:
        """Start the arrival subscription, or say why not."""
        if not attachment.is_current:
            return self._failure(
                request.id, ErrorCode.SESSION_REPLACED, _REPLACED_REASON
            )
        core = self._core

        async def send(event: Event) -> None:
            if not await self._send(ws, event):
                raise ClientGoneError

        def died(_exc: BaseException) -> None:
            reason = f"subscription failed in biffd; {LOG_HINT}"
            Supervised.spawn(self._close(ws, _INTERNAL_ERROR, reason), "close")

        subscription = Subscription(
            attachment.session, send, core.seq, core.poll_interval
        )
        task = Supervised.spawn(subscription.run(), "subscription", died)
        if not attachment.hold_subscription(task):
            task.cancel()
            msg = "this connection is already subscribed"
            return self._failure(request.id, ErrorCode.ALREADY_SUBSCRIBED, msg)
        return Reply(id=request.id, result={"subscribed": True})

    def _end_replaced(self, ws: ServerConnection) -> None:
        """Close the connection of a run a newer run has replaced."""
        Supervised.spawn(
            self._close(ws, _POLICY_VIOLATION, _REPLACED_REASON), "close replaced run"
        )

    def _end_ended(self, ws: ServerConnection) -> None:
        """Close the connection of a session that was ended."""
        Supervised.spawn(
            self._close(ws, _POLICY_VIOLATION, _ENDED_REASON), "close ended session"
        )

    @staticmethod
    async def _close(ws: ServerConnection, code: int, reason: str) -> None:
        """Close *ws*; a close reason is at most 123 bytes, so it is clipped."""
        clipped = reason.encode()[:_MAX_CLOSE_REASON_BYTES].decode(errors="ignore")
        await ws.close(code, clipped)

    @staticmethod
    async def _send(ws: ServerConnection, frame: Reply | Event | Proof) -> bool:
        """Write *frame*; ``False`` means the client had already left."""
        try:
            await ws.send(Frames.encode(frame))
        except ConnectionClosed:
            logger.debug("client left before the frame was sent", exc_info=True)
            return False
        return True
