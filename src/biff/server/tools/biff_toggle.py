"""Activation toggle — ``biff(enabled=True|False)``.

Allows users to enable or disable biff for the current repo from within
a Claude Code session.  Writes ``.biff.local`` (gitignored, per-user)
and advises a restart for changes to take effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.config import is_enabled, write_biff_local

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

        Writes ``.biff.local`` with the ``enabled`` flag.  If enabling
        and no ``.biff`` team config exists, creates a minimal one using
        identity and relay defaults from the current server config.

        Returns guidance to restart Claude Code for changes to take effect.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        if enabled and not (repo_root / ".biff").exists():
            # Create minimal .biff with current config defaults
            from biff.__main__ import build_biff_toml  # noqa: PLC0415
            from biff.config import DEMO_RELAY_URL  # noqa: PLC0415
            from biff.relay import atomic_write  # noqa: PLC0415

            relay_url = state.config.relay_url or DEMO_RELAY_URL
            content = build_biff_toml(list(state.config.team), relay_url)
            atomic_write(repo_root / ".biff", content)

        write_biff_local(repo_root, enabled=enabled)

        currently = is_enabled(repo_root)
        verb = "enabled" if currently else "disabled"
        return f"biff {verb}. Restart Claude Code for changes to take effect."
