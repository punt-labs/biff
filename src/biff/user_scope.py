"""Strip the legacy user-scope ``@``-import on uninstall.

``tool-enable-disable.md`` §2.6: there is no user-scope import, and ``install``
never writes ``~/.claude/CLAUDE.md``. If the file contains
``@~/.punt-labs/biff/CLAUDE.md``, ``uninstall`` removes that line and
leaves any deposited guide file dormant (§2.9 — do not delete what this run did
not create).
"""

from __future__ import annotations

from pathlib import Path
from typing import Self, final

from biff.plugins.claude_code.claude_md import ClaudeMdImport

__all__ = ["USER_IMPORT_LINE", "UserScope"]

# The legacy user-scope import string, the ONE source of truth for every
# module that prunes or prints it (§2.4 requires byte-identical output across
# writers). Points at the legacy deposited guide via a literal ``~``.
USER_IMPORT_LINE = "@~/.punt-labs/biff/CLAUDE.md"


@final
class UserScope:
    """Owns the legacy user-scope import line in ``~/.claude/CLAUDE.md``.

    The host path is injectable so tests never touch the real home directory;
    the default resolves under ``~``.
    """

    __slots__ = ("_host",)

    _host: Path

    def __new__(cls, *, host_claude_md: Path | None = None) -> Self:
        """Create a host configuration manager for the selected file."""
        self = super().__new__(cls)
        self._host = host_claude_md or (Path.home() / ".claude" / "CLAUDE.md")
        return self

    def uninstall(self) -> bool:
        """Prune the legacy import line. Return ``True`` if ``CLAUDE.md`` changed.

        A guide file deposited by an older release is left in place (§2.9):
        removal is deliberate, not a side effect of uninstall.
        """
        return ClaudeMdImport(self._host, USER_IMPORT_LINE).prune()
