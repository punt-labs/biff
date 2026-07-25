"""User-scope install: deposit biff's agent guide and register its ``@``-import.

Biff is a global tool (``tool-enable-disable.md`` §2.6): its guidance is
universal, so it registers the user-scope import once at ``install`` rather than
per-repo. ``install`` deposits ``~/.punt-labs/biff/CLAUDE.md`` (the agent guide,
§2.5) and adds ``@~/.punt-labs/biff/CLAUDE.md`` to ``~/.claude/CLAUDE.md``;
``uninstall`` removes the import line but leaves the deposited guide dormant
(§2.9 — do not delete what this run did not create).
"""

from __future__ import annotations

import importlib.resources
from dataclasses import dataclass
from pathlib import Path
from typing import Self, final

from biff.claude_md import ClaudeMdImport

__all__ = ["USER_IMPORT_LINE", "UserScope", "UserScopeResult"]

# The canonical user-scope import string, the ONE source of truth for every
# module that registers, prunes, reports, or prints it (§2.4 requires
# byte-identical output across writers). Points at the deposited guide via a
# literal ``~``; Claude Code expands it at read time.
USER_IMPORT_LINE = "@~/.punt-labs/biff/CLAUDE.md"
_GUIDE_RESOURCE = "user-claude.md"


@dataclass(frozen=True, slots=True)
class UserScopeResult:
    """What an ``install`` run changed, for a caller to report."""

    guide_written: bool
    import_registered: bool


@final
class UserScope:
    """Owns biff's user-scope guide file and its import line in ``CLAUDE.md``.

    Paths are injectable so tests never touch the real home directory; the
    defaults resolve under ``~``.
    """

    __slots__ = ("_guide", "_host")

    _host: Path
    _guide: Path

    def __new__(
        cls,
        *,
        host_claude_md: Path | None = None,
        guide_path: Path | None = None,
    ) -> Self:
        self = super().__new__(cls)
        self._host = host_claude_md or (Path.home() / ".claude" / "CLAUDE.md")
        self._guide = guide_path or (Path.home() / ".punt-labs" / "biff" / "CLAUDE.md")
        return self

    def install(self) -> UserScopeResult:
        """Deposit the guide and register the import line. Idempotent."""
        guide_written = self._deposit_guide()
        import_registered = ClaudeMdImport(self._host, USER_IMPORT_LINE).register()
        return UserScopeResult(
            guide_written=guide_written,
            import_registered=import_registered,
        )

    def uninstall(self) -> bool:
        """Prune the import line. Return ``True`` if ``CLAUDE.md`` changed.

        The deposited guide is left in place (§2.9): removal is deliberate, not
        a side effect of uninstall.
        """
        return ClaudeMdImport(self._host, USER_IMPORT_LINE).prune()

    def _deposit_guide(self) -> bool:
        """Write the bundled guide to the guide path. Return ``True`` if changed.

        Overwrites wholesale (§2.2 vendored zone); a no-op when the deposited
        bytes already match, so re-running ``install`` never churns the file.
        The freshness check compares *bytes* (not decoded text) so a tampered,
        non-UTF-8 guide on disk is simply overwritten rather than crashing the
        compare with ``UnicodeDecodeError``.
        """
        content = self._bundled_guide()
        want = content.encode("utf-8")
        if self._guide.is_file() and self._guide.read_bytes() == want:
            return False
        self._guide.parent.mkdir(parents=True, exist_ok=True)
        self._guide.write_bytes(want)
        return True

    @staticmethod
    def _bundled_guide() -> str:
        """Read the agent guide shipped in the ``biff.data`` package."""
        return (
            importlib.resources.files("biff.data")
            .joinpath(_GUIDE_RESOURCE)
            .read_text(encoding="utf-8")
        )
