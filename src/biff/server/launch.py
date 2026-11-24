"""Process launch configuration shared by MCP transports."""

from __future__ import annotations

from dataclasses import dataclass

from biff.config import ResolvedConfig, is_enabled
from biff.plugins.claude_code.statusline import UNREAD_DIR
from biff.server.state import RepoToggle, ServerState
from biff.server.state_factory import StateBlueprint


@dataclass(frozen=True)
class McpLaunch:
    """Build session resources from resolved configuration."""

    _resolved: ResolvedConfig
    toggle: RepoToggle

    def state(self) -> ServerState:
        """Create initially unbound resources; the lifespan establishes presence."""
        resolved = self._resolved
        return StateBlueprint(
            resolved.config,
            resolved.data_dir,
            self.toggle,
            unread_dir=UNREAD_DIR,
            dormant=not is_enabled(resolved.repo_root),
            repo_root=resolved.repo_root,
            repo_common_root=resolved.repo_common_root,
        ).build()
