"""Command-line adapters for Biff's HTTP and stdio MCP transports."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal

import typer

from biff._cli_runtime import McpInvocation
from biff.config import RELAY_URL_UNSET, load_mcp_config
from biff.server.transport_runtime import McpLifecycleHandler, McpServerBuilder
from biff.usage import McpUsageMiddleware

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import RepoToggle, ServerState

logger = logging.getLogger(__name__)

_User = Annotated[
    str | None, typer.Option(help="Your username. Auto-detected from GitHub CLI.")
]
_DataDir = Annotated[
    Path | None,
    typer.Option(help="Data directory. Auto-computed as {prefix}/biff/{repo}."),
]
_RelayUrl = Annotated[
    str | None,
    typer.Option(help="Relay URL override. Empty string forces local relay."),
]
_Prefix = Annotated[
    Path, typer.Option(help="Base path for data directory (default: /tmp).")
]
_DEFAULT_PREFIX = Path("/tmp")  # noqa: S108


@dataclass(frozen=True, slots=True)
class McpServerOptions:
    """The options and server construction shared by both transports."""

    user: str | None
    data_dir: Path | None
    relay_url: str | None
    prefix: Path
    transport: Literal["stdio", "http"] = "stdio"

    def create_server(
        self, toggle: RepoToggle, *, default_user: str | None = None
    ) -> FastMCP[ServerState]:
        """Resolve configuration and build an unbound MCP server."""
        resolved = load_mcp_config(
            user_override=self.user or default_user,
            data_dir_override=self.data_dir,
            relay_url_override=(
                self.relay_url if self.relay_url is not None else RELAY_URL_UNSET
            ),
            prefix=self.prefix,
        )

        return McpServerBuilder(
            resolved, self.user, self.transport, toggle
        ).create_server()

    def _observed_server(self, ctx: typer.Context) -> FastMCP[ServerState]:
        invocation = ctx.find_object(McpInvocation)
        if invocation is None:
            msg = "the root CLI callback did not register an MCP invocation"
            raise RuntimeError(msg)
        server = self.create_server(invocation.toggle, default_user=invocation.user)
        server.add_middleware(McpUsageMiddleware(invocation.usage))
        return server

    @classmethod
    def serve(
        cls,
        ctx: typer.Context,
        user: _User = None,
        data_dir: _DataDir = None,
        relay_url: _RelayUrl = None,
        prefix: _Prefix = _DEFAULT_PREFIX,
        host: Annotated[str, typer.Option(help="HTTP host.")] = "127.0.0.1",
        port: Annotated[int, typer.Option(help="HTTP port.")] = 8419,
    ) -> None:
        """Start the biff MCP server (HTTP transport)."""
        server = cls(user, data_dir, relay_url, prefix, "http")._observed_server(ctx)
        logger.info("Starting Biff MCP server (HTTP): port=%d", port)
        typer.echo(f"Starting biff MCP server on http://{host}:{port}")
        http = {"log_config": McpLifecycleHandler.configuration()}
        server.run(transport="http", host=host, port=port, uvicorn_config=http)

    @classmethod
    def mcp(
        cls,
        ctx: typer.Context,
        user: _User = None,
        data_dir: _DataDir = None,
        relay_url: _RelayUrl = None,
        prefix: _Prefix = _DEFAULT_PREFIX,
    ) -> None:
        """Start the biff MCP server (stdio transport)."""
        server = cls(user, data_dir, relay_url, prefix)._observed_server(ctx)
        with McpLifecycleHandler.stdio():
            server.run(transport="stdio")
