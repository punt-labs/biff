"""Activation toggle -- ``biff(action="enable"|"disable")``.

Lets an agent turn biff on or off for the current repo from within a
Claude Code session.  ``action="enable"`` writes the committed
enablement marker ``.punt-labs/biff/enabled`` and ``action="disable"``
removes it (tool-enable-disable.md §2.7) -- the single source of truth
shared with the ``biff enable`` / ``biff disable`` CLI verbs.  The marker
is a tracked repo file; the user commits it via a PR like any other
change, so this tool never runs git.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from biff.config import (
    remove_enabled_marker,
    write_enabled_marker,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff enable/disable tool."""

    @mcp.tool(
        name="biff",
        description=(
            "Enable or disable biff for this repo. "
            "Use action='enable' to activate, action='disable' to deactivate."
        ),
    )
    async def biff(action: Literal["enable", "disable"]) -> str:
        """Enable or disable biff for the current repository.

        Creates (enable) or removes (disable) the committed marker
        ``.punt-labs/biff/enabled``.  The marker is a tracked repo file
        -- commit it via a PR for the change to take effect for every
        contributor.  Returns guidance to restart Claude Code.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        if action == "enable":
            write_enabled_marker(repo_root)
            return (
                "biff enabled. Commit .punt-labs/biff/enabled and restart "
                "Claude Code for changes to take effect."
            )
        remove_enabled_marker(repo_root)
        return (
            "biff disabled. Commit the removal of .punt-labs/biff/enabled and "
            "restart Claude Code for changes to take effect."
        )
