"""Claude Code's :class:`~biff.plugins.contract.Installer`."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Final, Self, final

from biff.plugins.claude_code.claude_md import UNCLOSED_FENCE, ClaudeMdImport
from biff.plugins.claude_code.claude_permissions import (
    PermissionChange,
    ProjectPermissions,
)
from biff.plugins.claude_code.doctor_checks import DoctorChecks
from biff.plugins.claude_code.statusline import SETTINGS_PATH

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from biff.plugins.contract import CheckResult

__all__ = ["ClaudeCodeInstaller"]

_GUIDE_IMPORT: Final = "@.punt-labs/biff/CLAUDE.md"


@final
class ClaudeCodeInstaller:
    """What biff installs for Claude Code: permission rules, guide import, checks."""

    __slots__ = ()

    def __new__(cls) -> Self:
        """Create the installer; it holds no state."""
        return super().__new__(cls)

    def preflight(self, root: Path) -> PermissionChange:
        """Refuse when ``CLAUDE.md`` ends inside an unclosed code fence."""
        if self._guide(root).append_would_be_fenced():
            return PermissionChange(
                error=UNCLOSED_FENCE, fix="Close the code fence in CLAUDE.md"
            )
        return PermissionChange()

    def grant(self, root: Path) -> PermissionChange:
        """Add biff's rules to the repo's ``.claude/settings.json``."""
        return ProjectPermissions(root).grant()

    def revoke(
        self, root: Path, rules: Iterable[str] | None = None
    ) -> PermissionChange:
        """Remove *rules* (default: all of biff's) from ``.claude/settings.json``."""
        return ProjectPermissions(root).revoke(rules)

    def register_guide(self, root: Path) -> bool:
        """Append the ``@`` import of biff's guide to the repo's ``CLAUDE.md``."""
        return self._guide(root).register()

    def unregister_guide(self, root: Path) -> bool:
        """Remove that import from ``CLAUDE.md``."""
        return self._guide(root).prune()

    def check(self, user_override: str | None) -> Iterable[CheckResult]:
        """Report the status line and what blocks channels (informational)."""
        return DoctorChecks(SETTINGS_PATH, os.environ, user_override).all()

    @staticmethod
    def _guide(root: Path) -> ClaudeMdImport:
        return ClaudeMdImport(root / "CLAUDE.md", _GUIDE_IMPORT)
