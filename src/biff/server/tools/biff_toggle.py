"""Activation toggle -- ``biff(enabled=True|False)``.

Allows users to enable or disable biff for the current repo from within
a Claude Code session.  Writes ``.punt-labs/biff/config.local.yaml``
(gitignored, per-user) and advises a restart for changes to take effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff._stdlib import is_enabled
from biff.config import (
    ensure_gitignore_yaml,
    write_yaml_local_enabled,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

    from biff.server.state import ServerState


def register(mcp: FastMCP[ServerState], state: ServerState) -> None:
    """Register the biff toggle tool."""

    @mcp.tool(
        name="biff",
        description=(
            "Enable or disable biff for this repo. "
            "Use enabled=true to activate, enabled=false to deactivate."
        ),
    )
    async def biff(enabled: bool) -> str:  # noqa: FBT001
        """Toggle biff activation for the current repository.

        Writes ``.punt-labs/biff/config.local.yaml`` with the
        ``enabled`` flag.  No ``config.yaml`` is created -- zero-config
        mode uses derived defaults.

        Returns guidance to restart Claude Code for changes to take effect.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        write_yaml_local_enabled(repo_root, enabled=enabled)
        ensure_gitignore_yaml(repo_root)

        currently = is_enabled(repo_root)
        verb = "enabled" if currently else "disabled"
        return f"biff {verb}. Restart Claude Code for changes to take effect."
