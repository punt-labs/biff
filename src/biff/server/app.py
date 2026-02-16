"""FastMCP application factory.

``create_server`` builds a fully configured FastMCP instance with all
tools registered. The returned server is run via ``mcp.run(transport=...)``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastmcp import FastMCP

from biff.server.state import ServerState
from biff.server.tools import register_all_tools
from biff.server.tools._descriptions import poll_inbox


def create_server(state: ServerState) -> FastMCP[ServerState]:
    """Create a FastMCP server with all biff tools registered.

    The returned server is ready to run via ``mcp.run(transport=...)``.
    Starts a background inbox poller that keeps the tool description
    and status file in sync with incoming messages.
    """

    @asynccontextmanager
    async def lifespan(mcp: FastMCP[ServerState]) -> AsyncIterator[ServerState]:
        task = asyncio.create_task(poll_inbox(mcp, state))
        try:
            yield state
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            with suppress(Exception):
                await state.relay.delete_session(state.session_key)
            await state.relay.close()

    mcp: FastMCP[ServerState] = FastMCP(
        "biff",
        instructions=(
            "Biff is a communication tool for software engineers. "
            "Use these tools to send messages, check presence, "
            "and coordinate with your team.\n\n"
            "All biff tool output is pre-formatted plain text using unicode "
            "characters for alignment. Always emit biff output verbatim — "
            "never reformat, never convert to markdown tables, never wrap "
            "in code fences or boxes."
        ),
        lifespan=lifespan,
    )

    register_all_tools(mcp, state)
    return mcp
