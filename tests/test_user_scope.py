"""Tests for user-scope install/uninstall (tool-enable-disable.md §2.5/§2.6)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.user_scope import USER_IMPORT_LINE, UserScope

if TYPE_CHECKING:
    from pathlib import Path

_LINE = "@~/.punt-labs/biff/CLAUDE.md"


def test_canonical_import_line_is_the_shared_constant() -> None:
    # One source of truth for the §2.4 canonical string; doctor and the CLI
    # import this same constant rather than re-spelling the literal.
    assert USER_IMPORT_LINE == _LINE


def _scope(tmp_path: Path) -> UserScope:
    return UserScope(
        host_claude_md=tmp_path / ".claude" / "CLAUDE.md",
        guide_path=tmp_path / ".punt-labs" / "biff" / "CLAUDE.md",
    )


class TestInstall:
    def test_deposits_guide_and_registers_import(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        result = scope.install()

        assert result.guide_written is True
        assert result.import_registered is True

        guide = tmp_path / ".punt-labs" / "biff" / "CLAUDE.md"
        host = tmp_path / ".claude" / "CLAUDE.md"
        assert guide.is_file()
        assert "Biff (team messaging)" in guide.read_text()
        assert host.read_text().rstrip("\n").endswith(_LINE)

    def test_idempotent(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        scope.install()
        second = scope.install()

        assert second.guide_written is False
        assert second.import_registered is False

        host = tmp_path / ".claude" / "CLAUDE.md"
        assert host.read_text().count(_LINE) == 1

    def test_preserves_existing_user_prose(self, tmp_path: Path) -> None:
        host = tmp_path / ".claude" / "CLAUDE.md"
        host.parent.mkdir(parents=True)
        host.write_text("# My global rules\n\nAlways use uv.\n")

        _scope(tmp_path).install()

        text = host.read_text()
        assert text.startswith("# My global rules\n\nAlways use uv.\n")
        assert text.rstrip("\n").endswith(_LINE)

    def test_non_utf8_guide_on_disk_is_overwritten(self, tmp_path: Path) -> None:
        # A tampered, non-UTF-8 guide must be overwritten, not crash the
        # byte-compare with UnicodeDecodeError.
        guide = tmp_path / ".punt-labs" / "biff" / "CLAUDE.md"
        guide.parent.mkdir(parents=True)
        guide.write_bytes(b"\xff\xfe garbage\n")

        result = _scope(tmp_path).install()

        assert result.guide_written is True
        assert "Biff (team messaging)" in guide.read_text()


class TestUninstall:
    def test_prunes_import_leaves_guide(self, tmp_path: Path) -> None:
        scope = _scope(tmp_path)
        scope.install()

        assert scope.uninstall() is True

        host = tmp_path / ".claude" / "CLAUDE.md"
        guide = tmp_path / ".punt-labs" / "biff" / "CLAUDE.md"
        assert _LINE not in host.read_text()
        # Dormant: the deposited guide survives uninstall (§2.9).
        assert guide.is_file()

    def test_uninstall_without_install_is_noop(self, tmp_path: Path) -> None:
        assert _scope(tmp_path).uninstall() is False
