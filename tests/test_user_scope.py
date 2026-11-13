"""Tests for the legacy user-scope import strip (tool-enable-disable.md §2.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.claude_md import ClaudeMdImport
from biff.user_scope import USER_IMPORT_LINE, UserScope

if TYPE_CHECKING:
    from pathlib import Path

_LINE = "@~/.punt-labs/biff/CLAUDE.md"


def test_canonical_import_line_is_the_shared_constant() -> None:
    # One source of truth for the §2.4 canonical string.
    assert USER_IMPORT_LINE == _LINE


def _legacy_host(tmp_path: Path, prose: str = "") -> Path:
    """Seed a ``~/.claude/CLAUDE.md`` carrying the line an older release wrote."""
    host = tmp_path / ".claude" / "CLAUDE.md"
    host.parent.mkdir(parents=True)
    host.write_text(prose)
    ClaudeMdImport(host, _LINE).register()
    return host


class TestUninstall:
    def test_prunes_legacy_import_leaves_guide(self, tmp_path: Path) -> None:
        host = _legacy_host(tmp_path)
        guide = tmp_path / ".punt-labs" / "biff" / "CLAUDE.md"
        guide.parent.mkdir(parents=True)
        guide.write_text("legacy guide\n")

        assert UserScope(host_claude_md=host).uninstall() is True

        assert _LINE not in host.read_text()
        # Dormant: a guide deposited by an older release survives (§2.9).
        assert guide.is_file()

    def test_preserves_user_prose(self, tmp_path: Path) -> None:
        host = _legacy_host(tmp_path, "# My global rules\n\nAlways use uv.\n")

        UserScope(host_claude_md=host).uninstall()

        assert host.read_text().startswith("# My global rules\n\nAlways use uv.\n")

    def test_uninstall_without_legacy_line_is_noop(self, tmp_path: Path) -> None:
        host = tmp_path / ".claude" / "CLAUDE.md"
        assert UserScope(host_claude_md=host).uninstall() is False
