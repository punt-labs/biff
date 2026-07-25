"""Tests for the user-scope ``@``-import writer (tool-enable-disable.md §2.4)."""

from __future__ import annotations

import stat
from typing import TYPE_CHECKING

import pytest

from biff.claude_md import ClaudeMdImport

if TYPE_CHECKING:
    from pathlib import Path

_LINE = "@~/.punt-labs/biff/CLAUDE.md"


class TestConstructorValidation:
    """The import line is validated at the boundary (PY-EH-1)."""

    def test_empty_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="non-empty"):
            ClaudeMdImport(tmp_path / "CLAUDE.md", "")

    def test_missing_at_prefix_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="begin with '@'"):
            ClaudeMdImport(tmp_path / "CLAUDE.md", ".punt-labs/biff/CLAUDE.md")

    def test_embedded_newline_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="single line"):
            ClaudeMdImport(tmp_path / "CLAUDE.md", "@a\n@b")

    def test_surrounding_whitespace_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="whitespace"):
            ClaudeMdImport(tmp_path / "CLAUDE.md", "  @x  ")


class TestRegister:
    """``register`` appends exactly one bare line, idempotently."""

    def test_creates_missing_file(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert host.read_text() == f"{_LINE}\n"
        assert stat.S_IMODE(host.stat().st_mode) == 0o644

    def test_idempotent(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert imp.register() is False
        assert host.read_text().count(_LINE) == 1

    def test_appends_to_existing_prose(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("# My rules\n\nBe kind.\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert host.read_text() == f"# My rules\n\nBe kind.\n{_LINE}\n"

    def test_ensures_separating_newline(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("no trailing newline")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert host.read_text() == f"no trailing newline\n{_LINE}\n"

    def test_terminator_insensitive_match(self, tmp_path: Path) -> None:
        # A CRLF host already carrying the line must not get a duplicate.
        host = tmp_path / "CLAUDE.md"
        host.write_bytes(f"intro\r\n{_LINE}\r\n".encode())
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is False
        assert host.read_bytes().count(_LINE.encode()) == 1

    def test_skips_line_inside_code_fence(self, tmp_path: Path) -> None:
        # A matching line inside a fenced block is inert markdown, not an
        # import; register must still append a real top-level line.
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"```text\n{_LINE}\n```\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert host.read_text() == f"```text\n{_LINE}\n```\n{_LINE}\n"


class TestBytePreservation:
    """Every byte outside the appended line survives verbatim."""

    def test_lf_preserved(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_bytes(b"a\nb\n")
        ClaudeMdImport(host, _LINE).register()
        assert host.read_bytes() == f"a\nb\n{_LINE}\n".encode()

    def test_crlf_preserved_and_appended_line_uses_crlf(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_bytes(b"a\r\nb\r\n")
        ClaudeMdImport(host, _LINE).register()
        assert host.read_bytes() == f"a\r\nb\r\n{_LINE}\r\n".encode()

    def test_preserves_existing_mode(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("x\n")
        host.chmod(0o600)
        ClaudeMdImport(host, _LINE).register()
        assert stat.S_IMODE(host.stat().st_mode) == 0o600


class TestSymlink:
    """A symlinked host is followed to its real file; the link is preserved."""

    def test_writes_through_symlink(self, tmp_path: Path) -> None:
        real = tmp_path / "real-CLAUDE.md"
        real.write_text("base\n")
        link = tmp_path / "CLAUDE.md"
        link.symlink_to(real)

        ClaudeMdImport(link, _LINE).register()

        assert link.is_symlink()
        assert real.read_text() == f"base\n{_LINE}\n"


class TestPrune:
    """``disable``/``uninstall`` removes every matching top-level line."""

    def test_removes_line(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"keep\n{_LINE}\ntail\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.prune() is True
        assert host.read_text() == "keep\ntail\n"

    def test_absent_is_noop(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("keep\n")
        assert ClaudeMdImport(host, _LINE).prune() is False
        assert host.read_text() == "keep\n"

    def test_collapses_duplicates(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"{_LINE}\nmid\n{_LINE}\n")
        assert ClaudeMdImport(host, _LINE).prune() is True
        assert _LINE not in host.read_text()
        assert host.read_text() == "mid\n"

    def test_keeps_line_inside_code_fence(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"```text\n{_LINE}\n```\n{_LINE}\n")
        assert ClaudeMdImport(host, _LINE).prune() is True
        # The fenced copy is inert and must survive; the top-level one goes.
        assert host.read_text() == f"```text\n{_LINE}\n```\n"


class TestIsRegistered:
    """``is_registered`` reflects top-level presence only."""

    def test_true_when_present(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"{_LINE}\n")
        assert ClaudeMdImport(host, _LINE).is_registered() is True

    def test_false_when_only_in_code_fence(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"```text\n{_LINE}\n```\n")
        assert ClaudeMdImport(host, _LINE).is_registered() is False

    def test_false_when_missing(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        assert ClaudeMdImport(host, _LINE).is_registered() is False
