"""Activation toggle -- ``biff(action="enable"|"disable")``.

Lets an agent turn biff on or off for the current repo from within a
Claude Code session.  Both actions route through
:class:`~biff.enablement.RepoEnablement`, the same definition the
``biff enable`` / ``biff disable`` CLI verbs use, so the two front-ends
produce an identical committed result (DES-052).

``action="enable"`` fully activates this clone: it writes the committed
marker ``.punt-labs/biff/enabled`` and CI notify workflow
``.github/workflows/biff-notify.yml`` AND deploys this clone's local
``.git/hooks`` biff dispatchers and allows biff's tools in the repo's
``.claude/settings.json``; ``action="disable"`` removes all four.
The committed files are tracked repo files the user commits via a PR like
any other change; the git hooks are per-clone and never committed.  This
tool invokes git only read-only (to resolve the hooks directory) and never
creates commits.
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
        """Enable or disable Biff for the current repository.

        Use action='enable' to activate this clone or 'disable' to deactivate it.
        Update the committed marker, notify workflow, agent guide import, and
        permissions, plus this clone's local git hooks. Commit tracked changes
        to apply repository policy to teammates; local hooks remain per-clone.

        Returns:
            A change report and restart guidance. Missing repositories, unresolved
            hook directories, and permission-edit failures are reported in text.

        Raises:
            OSError: An enablement artifact cannot be read, written, or removed.
            ValueError: The bundled workflow template cannot be rendered safely.
        """
        repo_root = state.repo_root
        if repo_root is None:
            return "Error: not in a git repository."

        if action == "enable":
            change = RepoEnablement(repo_root).enable()
            if not change.git_hooks_resolved:
                # Fail loud, identical to the CLI `biff enable` surface: enable
                # wrote nothing (marker included), so never claim success.
                from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE  # noqa: PLC0415

                return HOOKS_DIR_UNRESOLVED_NOTICE
            return "\n".join(change.enable_report())
        return "\n".join(RepoEnablement(repo_root).disable().disable_report())
