"""FastMCP application factory.

``create_server`` builds a fully configured FastMCP instance with all
tools registered. The returned server is run via ``mcp.run(transport=...)``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastmcp import FastMCP

from biff.server.state import ServerState
from biff.server.tools import register_all_tools


def create_server(state: ServerState) -> FastMCP[ServerState]:
    """Create a FastMCP server with all biff tools registered.

    The returned server is ready to run via ``mcp.run(transport=...)``.
    """

    @asynccontextmanager
    async def lifespan(_mcp: FastMCP[ServerState]) -> AsyncIterator[ServerState]:
        yield state

    mcp: FastMCP[ServerState] = FastMCP(
        "biff",
        instructions=(
            "Biff is a communication tool for software engineers. "
            "Use these tools to send messages, check presence, "
            "and coordinate with your team."
        ),
        lifespan=lifespan,
    )

    register_all_tools(mcp, state)
    return mcp
