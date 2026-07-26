"""Activation toggle -- ``biff(action="enable"|"disable")``.

Lets an agent turn biff on or off for the current repo from within a
Claude Code session.  Both actions route through
:class:`~biff.enablement.RepoEnablement`, the same definition the
``biff enable`` / ``biff disable`` CLI verbs use, so the two front-ends
produce an identical committed result (DES-052, biff-j5u).

``action="enable"`` fully activates this clone: it writes the committed
marker ``.punt-labs/biff/enabled`` and CI notify workflow
``.github/workflows/biff-notify.yml`` AND deploys this clone's local
``.git/hooks`` biff dispatchers; ``action="disable"`` removes all three.
The committed files are tracked repo files the user commits via a PR like
any other change; the git hooks are per-clone and never committed.  This
tool never runs git.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from biff.enablement import RepoEnablement

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

        Enable fully activates this clone; disable deactivates it.  Writes
        (enable) or removes (disable) the two committed enablement artifacts
        -- the marker ``.punt-labs/biff/enabled`` and the CI notify workflow
        ``.github/workflows/biff-notify.yml`` -- plus this clone's local
        ``.git/hooks`` biff dispatchers.  Commit the tracked files via a PR
        for the change to take effect for every contributor.  Returns
        guidance to restart Claude Code.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        if action == "enable":
            RepoEnablement(repo_root).enable()
            return (
                "biff enabled. Commit .punt-labs/biff/enabled and "
                ".github/workflows/biff-notify.yml, then restart Claude Code "
                "for changes to take effect."
            )
        RepoEnablement(repo_root).disable()
        return (
            "biff disabled. Commit the removal of .punt-labs/biff/enabled and "
            ".github/workflows/biff-notify.yml, then restart Claude Code "
            "for changes to take effect."
        )
