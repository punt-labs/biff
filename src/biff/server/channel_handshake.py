"""What the channel loop tells the sender and the model when a client connects."""

from __future__ import annotations

import logging
from contextlib import suppress
from typing import TYPE_CHECKING, Protocol

from biff.server.channel import ChannelLink
from biff.server.tools import _descriptions

if TYPE_CHECKING:
    from collections.abc import Mapping
    from typing import Any

    from fastmcp import Context
    from mcp.shared.dispatcher import DispatchContext, OnNotify, OnRequest
    from mcp.shared.transport_context import TransportContext

logger = logging.getLogger(__name__)


class Instructed(Protocol):
    """A server whose instructions text can be extended."""

    instructions: str | None


class ClientGate(Protocol):
    """What the server tells the channel's sender about the client."""

    @property
    def instructions(self) -> str:
        """The paragraph the server instructions gain in channel mode."""
        ...

    def serve(self, link: ChannelLink) -> None:
        """Turn channel mode on for the connection *link* carries."""

    def initializing(self) -> None:
        """Close the gate: the client sent ``initialize`` and is not yet ready."""

    def initialized(self) -> None:
        """Open the gate: the client sent ``notifications/initialized``."""


class ChannelHandshake:
    """Steps the channel loop takes when a client connects."""

    @staticmethod
    def open(server: Instructed, link: ChannelLink, emitter: ClientGate | None) -> None:
        """Make *link* the one connection, and tell *emitter* and the model about it.

        The server's instructions gain the emitter's paragraph on untrusted
        bodies; the emitter learns which link to send on.
        """
        _descriptions.handshake_delivery.serve_single_connection(link)
        if emitter is not None:
            emitter.serve(link)
            base = server.instructions or ""
            server.instructions = f"{base}\n\n{emitter.instructions}"

    @staticmethod
    def watch(
        emitter: ClientGate | None, on_request: OnRequest, on_notify: OnNotify
    ) -> tuple[OnRequest, OnNotify]:
        """Wrap the dispatcher's callbacks so *emitter* hears when the client is ready.

        ``initialize`` closes the sender's gate, including a second one; the
        client's ``notifications/initialized`` opens it, after the SDK has
        handled it.  With no emitter the callbacks are returned as they are.
        """
        if emitter is None:
            return on_request, on_notify

        async def request(
            dctx: DispatchContext[TransportContext],
            method: str,
            params: Mapping[str, Any] | None,
        ) -> dict[str, Any]:
            if method == "initialize":
                emitter.initializing()
            return await on_request(dctx, method, params)

        async def notify(
            dctx: DispatchContext[TransportContext],
            method: str,
            params: Mapping[str, Any] | None,
        ) -> None:
            await on_notify(dctx, method, params)
            if method == "notifications/initialized":
                emitter.initialized()

        return request, notify

    @staticmethod
    async def capture(ctx: Context | None) -> None:
        """Store the session the handshake opened (``CaptureSession`` in the model).

        A session that cannot be read is logged, not hidden: in channel mode it
        is the only session there will be, and every notification would stay
        owed.
        """
        session = None
        with suppress(RuntimeError):
            session = ctx.session if ctx is not None else None
        if session is None:
            logger.warning("Handshake session not captured; changes stay owed")
        else:
            await _descriptions.handshake_delivery.capture_session(session)
