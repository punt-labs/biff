"""Spike: Validate that tools/list_changed updates tool descriptions in Claude Code.

This minimal MCP server tests whether Claude Code refreshes its tool list
when it receives a notifications/tools/list_changed notification.

The server exposes two tools:
1. `check_messages` -- description changes when messages arrive
2. `simulate_message` -- triggers a tool description update

Run with: uv run python spike/list_changed_server.py
Then register: claude mcp add --transport http biff-spike http://localhost:8419/mcp
"""

from __future__ import annotations

import asyncio
import logging

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("biff-spike")

# Global state
_unread_count: int = 0
_unread_preview: str = ""

mcp = FastMCP(
    "biff-spike",
    instructions="Biff spike server testing dynamic tool descriptions.",
)


def _make_check_tool(description: str) -> None:
    """Register the check_messages tool with the given description."""

    @mcp.tool(description=description)
    def check_messages() -> str:
        """Read unread messages."""
        global _unread_count, _unread_preview
        if _unread_count == 0:
            return "No new messages."
        count = _unread_count
        preview = _unread_preview
        _unread_count = 0
        _unread_preview = ""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _update_description("Check for new messages")
        else:
            loop.call_soon(lambda: _update_description("Check for new messages"))
        return f"You had {count} messages:\n{preview}"


def _update_description(new_description: str) -> None:
    """Update the check_messages tool description.

    FastMCP should automatically send notifications/tools/list_changed.
    """
    logger.info("Updating tool description to: %s", new_description)
    try:
        mcp.remove_tool("check_messages")
    except (KeyError, ValueError):
        logger.warning("Could not remove check_messages tool")
    _make_check_tool(new_description)


# Register initial tools
_make_check_tool("Check for new messages")


@mcp.tool(description="Simulate receiving a message (for spike testing)")
def simulate_message(from_user: str, body: str) -> str:
    """Simulate a message arrival to test dynamic tool description updates."""
    global _unread_count, _unread_preview
    _unread_count += 1
    _unread_preview += f"@{from_user}: {body}\n"
    new_desc = (
        f"Check messages ({_unread_count} unread: @{from_user} about {body[:40]})"
    )
    _update_description(new_desc)
    return f"Simulated message from @{from_user}. Tool description should update."


if __name__ == "__main__":
    import sys

    print("Starting biff-spike MCP server on http://localhost:8419")
    print(
        "Register with: claude mcp add --transport http"
        " biff-spike http://localhost:8419/mcp"
    )
    print("Then test:")
    print('  1. Call simulate_message(from_user="kai", body="auth is ready")')
    print("  2. Check if the check_messages tool description changed")
    sys.stdout.flush()
    mcp.run(transport="http", host="127.0.0.1", port=8419)
