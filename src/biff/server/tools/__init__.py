"""Tool registration for biff MCP server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.server.tools import biff_toggle, finger, plan, who

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register_all_tools(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register all biff tools on the server."""
    biff_toggle.register(mcp, state)
    finger.register(mcp, state)
    who.register(mcp, state)
    plan.register(mcp, state)
