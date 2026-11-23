"""Enable or disable biff in a repo and render the report the MCP tool returns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Self, final

from biff.enablement import RepoEnablement
from biff.git_hooks import HOOKS_DIR_UNRESOLVED_NOTICE

if TYPE_CHECKING:
    from pathlib import Path

    from biff.plugins.contract import PluginRegistry

__all__ = ["EnablementToggle"]


@final
class EnablementToggle:
    """Runs :class:`~biff.enablement.RepoEnablement` and returns its text report."""

    __slots__ = ("_plugins",)

    _plugins: PluginRegistry

    def __new__(cls, plugins: PluginRegistry) -> Self:
        """Create a toggle that runs each registered tool's installer."""
        self = super().__new__(cls)
        self._plugins = plugins
        return self

    def enable(self, root: Path) -> str:
        """Enable biff in *root*; the notice when no hooks directory resolves."""
        change = RepoEnablement(root, self._plugins).enable()
        if not change.git_hooks_resolved:
            # Fail loud, identical to the CLI `biff enable` surface: enable
            # wrote nothing (marker included), so never claim success.
            return HOOKS_DIR_UNRESOLVED_NOTICE
        return "\n".join(change.enable_report())

    def disable(self, root: Path) -> str:
        """Disable biff in *root* and return the report."""
        return "\n".join(RepoEnablement(root, self._plugins).disable().disable_report())
