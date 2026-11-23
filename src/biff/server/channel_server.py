"""Serve biff over stdio as a Claude Code channel (DES-087)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
from fastmcp.server.context import reset_transport, set_transport
from mcp.server.connection import Connection
from mcp.server.runner import ServerRunner, aclose_shielded
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import MCPError
from mcp.shared.jsonrpc_dispatcher import JSONRPCDispatcher
from mcp.types import INVALID_REQUEST, PROTOCOL_VERSION_META_KEY

from biff.server.channel import CHANNEL_CAPABILITY, ChannelLink, take_channel_mode
from biff.server.mcp_protocol import NotificationServer
from biff.server.tools import _descriptions

if TYPE_CHECKING:
    from collections.abc import Mapping

    from mcp.shared.dispatcher import DispatchContext
    from mcp.shared.transport_context import TransportContext

logger = logging.getLogger(__name__)

# The probe goes to the runner, which answers -32601 at a handshake version;
# that is the answer Claude Code falls back from.
_PASSED_TO_RUNNER = frozenset({"initialize", "server/discover"})


class ChannelServer[LifespanT](NotificationServer[LifespanT]):
    """A notification server that can serve stdio as a Claude Code channel.

    Channel mode drives FastMCP's ``_mcp_server`` directly, so
    ``pyproject.toml`` pins FastMCP's minor version.
    """

    def run_stdio(self) -> None:
        """Serve stdio, as a channel when the plugin asked for it.

        Reads ``BIFF_CHANNEL_MODE`` once and removes it from the environment.
        Every other caller keeps FastMCP's dual-era stdio loop.
        """
        channel_mode = take_channel_mode()
        logger.info("MCP stdio: channel_mode=%s", "on" if channel_mode else "off")
        if channel_mode:
            anyio.run(self.serve_channel_stdio, ChannelLink())
        else:
            self.run(transport="stdio")

    async def serve_channel_stdio(self, link: ChannelLink) -> None:
        """Serve stdio as a Claude Code channel, binding the connection to *link*.

        Claude Code opens with a 2026-07-28 ``server/discover`` probe and does
        not register a 2026-07-28 server as a channel, so this serves the
        handshake era only: the probe gets -32601, ``initialize`` succeeds, and
        a request carrying the 2026-07-28 envelope is refused.  It follows
        FastMCP's stdio runner, entering the SDK server's lifespan around the
        loop.
        """
        server = self._mcp_server
        options = server.create_initialization_options(
            experimental_capabilities={CHANNEL_CAPABILITY: {}}
        )
        _descriptions.handshake_delivery.keep_session = True
        token = set_transport("stdio")
        try:
            async with (
                server.lifespan(server) as lifespan_state,
                stdio_server() as (read_stream, write_stream),
            ):
                dispatcher: JSONRPCDispatcher[TransportContext] = JSONRPCDispatcher(
                    read_stream, write_stream, inline_methods=frozenset({"initialize"})
                )
                connection = Connection.for_loop(dispatcher)
                runner = ServerRunner(
                    server, connection, lifespan_state, init_options=options
                )
                link.bind(connection)
                try:
                    gate = _HandshakeOnly(runner)
                    await dispatcher.run(gate.on_request, runner.on_notify)
                finally:
                    link.unbind()
                    await aclose_shielded(connection)
                    # The client must see EOF, and this loop does not close it.
                    await write_stream.aclose()
        finally:
            reset_transport(token)


@dataclass(frozen=True, slots=True)
class _HandshakeOnly:
    """Refuse requests that carry the 2026-07-28 envelope, and log the handshake."""

    runner: ServerRunner[Any]

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
            msg = "this connection serves the handshake era; 2026-07-28 is refused"
            raise MCPError(code=INVALID_REQUEST, message=msg)
        result = await self.runner.on_request(dctx, method, params)
        if method == "initialize":
            logger.info(
                "MCP handshake: protocolVersion=%s channel_mode=on",
                self.runner.connection.protocol_version,
            )
        return result
