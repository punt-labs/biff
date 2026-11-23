"""Claude Code's :class:`~biff.plugins.contract.Installer`."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Final, Self, final

from biff.plugins.claude_code.claude_md import UNCLOSED_FENCE, ClaudeMdImport
from biff.plugins.claude_code.claude_permissions import (
    PermissionChange,
    ProjectPermissions,
)
from biff.plugins.claude_code.statusline import SETTINGS_PATH, read_settings
from biff.plugins.contract import CheckResult

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

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

    def check(self) -> tuple[CheckResult, ...]:
        """Report whether the status line is configured (informational)."""
        return (_check_statusline(),)

    @staticmethod
    def _guide(root: Path) -> ClaudeMdImport:
        return ClaudeMdImport(root / "CLAUDE.md", _GUIDE_IMPORT)


def _check_statusline() -> CheckResult:
    """Check status line is configured (informational)."""
    try:
        settings = read_settings(SETTINGS_PATH)
    except (json.JSONDecodeError, OSError):
        return CheckResult(
            name="Status line",
            passed=False,
            message="could not read settings.json",
            required=False,
        )
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict):
        cmd = status_line.get("command")  # pyright: ignore[reportUnknownMemberType,reportUnknownVariableType]
        if isinstance(cmd, str) and "biff statusline" in cmd:
            return CheckResult(
                name="Status line",
                passed=True,
                message="configured",
                required=False,
            )
    return CheckResult(
        name="Status line",
        passed=False,
        message="not configured (restart Claude Code to auto-install)",
        required=False,
    )
