"""Tool registration for biff MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools import finger, mesg, messaging, plan, tty, who

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register_all_tools(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register all biff tools on the server."""
    mesg.register(mcp, state)
    finger.register(mcp, state)
    messaging.register(mcp, state)
    who.register(mcp, state)
    plan.register(mcp, state)
    tty.register(mcp, state)
