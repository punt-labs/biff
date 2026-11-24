"""Serve biff over stdio as a Claude Code channel (DES-087)."""

from __future__ import annotations

import logging
from functools import cached_property
from typing import TYPE_CHECKING, Self

import anyio
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.connection import Connection
from mcp.server.runner import ServerRunner, aclose_shielded
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import INVALID_REQUEST, PROTOCOL_VERSION_META_KEY

from biff.server.channel import CHANNEL_CAPABILITY, ChannelLink
from biff.server.channel_handshake import ChannelHandshake, ClientGate
from biff.server.mcp_protocol import NotificationServer
from biff.usage import McpUsageMiddleware

if TYPE_CHECKING:
    from collections.abc import Mapping

    from fastmcp import Context
    from mcp.shared.dispatcher import DispatchContext
    from mcp.shared.transport_context import TransportContext

    from biff.usage import UsageCapture

logger = logging.getLogger(__name__)

# The probe goes to the runner, which answers -32601 at a handshake version;
# that is the answer Claude Code falls back from.
_PASSED_TO_RUNNER = frozenset({"initialize", "server/discover"})


class ChannelServer[LifespanT](NotificationServer[LifespanT]):
    """A notification server that can serve stdio as a Claude Code channel.

    Channel mode drives FastMCP's ``_mcp_server`` directly, so
    ``pyproject.toml`` pins FastMCP's minor version.
    """

    @cached_property
    def channel_link(self) -> ChannelLink:
        """The link to the live client, for whatever sends channel events."""
        return ChannelLink()

    _emitter: ClientGate | None = None

    def attach_emitter(self, emitter: ClientGate) -> None:
        """Name the emitter that sends events over this server's channel link."""
        self._emitter = emitter

    @staticmethod
    async def capture_handshake_session(ctx: Context | None) -> None:
        """Store the session the handshake opened; see :class:`ChannelHandshake`."""
        await ChannelHandshake.capture(ctx)

    def observe_usage(self, usage: UsageCapture) -> None:
        """Record each tool call's usage through *usage*."""
        self.add_middleware(McpUsageMiddleware(usage))

    def run_stdio(self, *, channel: bool) -> None:
        """Serve stdio, as a channel when the plugin asked for it.

        *channel* is the decision ``biff mcp`` took from
        ``BIFF_CHANNEL_MODE`` before it loaded any configuration.  Every other
        caller keeps FastMCP's dual-era stdio loop.
        """
        logger.info("MCP stdio: channel_mode=%s", "on" if channel else "off")
        if channel:
            anyio.run(self.serve_channel_stdio)
        else:
            self.run(transport="stdio")

    async def serve_channel_stdio(self) -> None:
        """Serve stdio as a Claude Code channel, attaching it to ``channel_link``.

        Claude Code opens with a 2026-07-28 ``server/discover`` probe and does
        not register a 2026-07-28 server as a channel, so this serves the
        handshake era only: the probe gets -32601, ``initialize`` succeeds, and
        a request carrying the 2026-07-28 envelope is refused.  It follows
        FastMCP's stdio runner, entering the SDK server's lifespan around the
        loop.
        """
        server = self._mcp_server
        link = self.channel_link
        options = server.create_initialization_options(
            experimental_capabilities={CHANNEL_CAPABILITY: {}}
        )
        ChannelHandshake.open(server, link, self._emitter)
        token = set_transport("stdio")
        try:
            async with (
                server.lifespan(server) as lifespan_state,
                stdio_server() as (read_stream, write_stream),
            ):
                dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
                    link.observe_reads(read_stream),
                    link.observe(write_stream),
                    inline_methods=frozenset({"initialize"}),
                    on_stream_exception=_log_malformed_input,
                )
                connection = Connection.for_loop(dispatcher)
                runner = ServerRunner(
                    server, connection, lifespan_state, init_options=options
                )
                try:
                    gate = _HandshakeOnly(runner)
                    hooks = ChannelHandshake.watch(
                        self._emitter, gate.on_request, runner.on_notify
                    )
                    await dispatcher.run(*hooks)
                finally:
                    link.detach()
                    try:
                        await aclose_shielded(connection)
                    finally:
                        # The client must see EOF; this loop does not close it.
                        await write_stream.aclose()
        except Exception:
            logger.exception("channel stdio transport failed")
            raise
        finally:
            reset_transport(token)


async def _log_malformed_input(exc: Exception) -> None:
    """Log a line the client sent that is not valid JSON-RPC."""
    logger.warning(
        "Ignored malformed client input: %s: %.200s", type(exc).__name__, exc
    )


class _HandshakeOnly[LifespanT]:
    """Refuse requests that carry the 2026-07-28 envelope, and log the handshake."""

    _runner: ServerRunner[LifespanT]
    _refused: set[str]

    def __new__(cls, runner: ServerRunner[LifespanT]) -> Self:
        """Gate *runner*; the first refusal is logged, later ones are not."""
        self = super().__new__(cls)
        self._runner = runner
        self._refused = set()
        return self

    async def on_request(
        self,
        dctx: DispatchContext[TransportContext],
        method: str,
        params: Mapping[str, object] | None,
    ) -> dict[str, object]:
        """Answer *method* on the handshake era, or refuse a 2026-07-28 request."""
        meta = params.get("_meta") if params else None
        has_envelope = isinstance(meta, dict) and PROTOCOL_VERSION_META_KEY in meta
        if has_envelope and method not in _PASSED_TO_RUNNER:
            if method not in self._refused:
                self._refused.add(method)
                logger.warning("Refused 2026-07-28 request: method=%s", method)
            msg = "this connection serves the handshake era; 2026-07-28 is refused"
            raise MCPError(code=INVALID_REQUEST, message=msg)
        try:
            result = await self._runner.on_request(dctx, method, params)
        except Exception:
            if method == "initialize":
                logger.exception("MCP initialize failed")
            raise
        if method == "initialize":
            logger.info(
                "MCP handshake: protocolVersion=%s channel_mode=on",
                self._runner.connection.protocol_version,
            )
        return result
