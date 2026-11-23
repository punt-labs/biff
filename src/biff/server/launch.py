"""Process launch configuration shared by MCP transports."""

from __future__ import annotations

from dataclasses import dataclass

from biff.caller_identity import CallerIdentity
from biff.config import ResolvedConfig, is_enabled
from biff.plugins.claude_code.statusline import UNREAD_DIR
from biff.server.state import RepoToggle, ServerState, create_state


@dataclass(frozen=True)
class McpLaunch:
    """Build session resources from resolved configuration and launch identity."""

    _resolved: ResolvedConfig
    caller: CallerIdentity | None
    toggle: RepoToggle

    def state(self) -> ServerState:
        """Create initially unbound resources; the lifespan establishes presence."""
        resolved = self._resolved
        return create_state(
            resolved.config,
            resolved.data_dir,
            unread_dir=UNREAD_DIR,
            dormant=not is_enabled(resolved.repo_root),
            repo_root=resolved.repo_root,
            repo_common_root=resolved.repo_common_root,
            toggle=self.toggle,
        )
