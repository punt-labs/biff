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

    def test_lone_cr_preserved_and_appended_line_uses_cr(self, tmp_path: Path) -> None:
        # Old-Mac lone-CR endings must survive and the appended line matches.
        host = tmp_path / "CLAUDE.md"
        host.write_bytes(b"a\rb\r")
        ClaudeMdImport(host, _LINE).register()
        assert host.read_bytes() == f"a\rb\r{_LINE}\r".encode()


class TestIndentedCodeBlock:
    """An indented (tab / 4-space) code block line is not a top-level import."""

    def test_indented_copy_is_not_registered(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"    {_LINE}\n")  # 4-space indent → code block
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is False
        # register appends a real top-level line; the indented copy survives.
        assert imp.register() is True
        assert host.read_text() == f"    {_LINE}\n{_LINE}\n"

    def test_prune_keeps_indented_copy(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"\t{_LINE}\n{_LINE}\n")  # tab-indented + top-level
        assert ClaudeMdImport(host, _LINE).prune() is True
        assert host.read_text() == f"\t{_LINE}\n"


class TestIndentedFenceIsNotDelimiter:
    """An indented (tab / 4+ space) ```/~~~ is inert code, not a fence.

    Per §2.4 an indented line is an indented-code line — it must NOT open or
    close a fenced block. Toggling fence state on it flips the parity for the
    rest of the file.
    """

    def test_indented_fence_inside_block_keeps_import_shielded(
        self, tmp_path: Path
    ) -> None:
        # @-import sits inside a real ```…``` block; an indented ``` inside must
        # not toggle fence state and wrongly expose the shielded copy.
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"```text\n    ```\n{_LINE}\n```\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is False
        assert imp.prune() is False
        assert host.read_text() == f"```text\n    ```\n{_LINE}\n```\n"

    def test_indented_fence_above_column0_import_stays_top_level(
        self, tmp_path: Path
    ) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("    ```text\nan indented code line\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.register() is True
        assert imp.is_registered() is True
        assert imp.register() is False
        assert host.read_text().count(_LINE) == 1


class TestLockPath:
    """The write lock must serialize ACROSS punt CLIs, not just biff-vs-biff.

    vox, quarry, and biff all write user-scope @-imports into the same
    ~/.claude/CLAUDE.md (§2.6). A tool-specific lock name buys no cross-writer
    exclusion — the lost-update race §2.4's lock exists to prevent. The name
    must be shared and tool-agnostic.
    """

    def test_lock_name_is_tool_agnostic(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        imp = ClaudeMdImport(host, _LINE)
        assert imp._lock_path == tmp_path / ".CLAUDE.md.punt-import.lock"


class TestFenceMarkerMatching:
    """A fenced block only closes on a same-marker, equal-or-longer delimiter.

    Per CommonMark a ```-block closes only on ```; a ~~~ line inside it is
    content, and a shorter run cannot close a longer opener. A mismatched
    delimiter must not toggle fence state and expose a shielded @-import.
    """

    def test_mismatched_marker_does_not_close_block(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        # ``` opens; the ~~~ line is content, not a close; ``` closes.
        host.write_text(f"```text\n~~~\n{_LINE}\n```\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is False
        assert imp.prune() is False
        assert host.read_text() == f"```text\n~~~\n{_LINE}\n```\n"

    def test_shorter_run_does_not_close_longer_fence(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        # 4-backtick opener; an inner 3-backtick line cannot close it.
        host.write_text(f"````text\n```\n{_LINE}\n````\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is False
        assert imp.prune() is False
        assert host.read_text() == f"````text\n```\n{_LINE}\n````\n"

    def test_tilde_block_shields_import(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"~~~\n{_LINE}\n~~~\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is False


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


class TestUnbalancedFence:
    """An unterminated fence must not swallow biff's column-0 import line.

    Only balanced ```…``` (or ~~~…~~~) pairs delimit a code block; a dangling
    opener in the user's prose must not misclassify the import as fenced, which
    would let register() duplicate and prune() fail to remove.
    """

    def test_unterminated_fence_above_does_not_hide_import(
        self, tmp_path: Path
    ) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_text("```text\nunclosed fence in user prose\n\nmore prose\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        assert imp.is_registered() is True
        # No duplicate on a second run.
        assert imp.register() is False
        assert host.read_text().count(_LINE) == 1
        # And it can be removed.
        assert imp.prune() is True
        assert _LINE not in host.read_text()

    def test_balanced_fence_still_shields_inner_line(self, tmp_path: Path) -> None:
        # A balanced fence below the import must still shield an inner copy.
        host = tmp_path / "CLAUDE.md"
        host.write_text(f"{_LINE}\n```text\n{_LINE}\n```\n")
        imp = ClaudeMdImport(host, _LINE)
        assert imp.is_registered() is True
        assert imp.prune() is True
        # Only the top-level line goes; the balanced-fenced copy survives.
        assert host.read_text() == f"```text\n{_LINE}\n```\n"


class TestNonUtf8Host:
    """A non-UTF-8 host file must neither crash nor corrupt (§2.4 byte-faithful)."""

    def test_register_preserves_invalid_utf8_bytes(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        # 0xE9 is 'é' in latin-1 — invalid as standalone UTF-8.
        host.write_bytes(b"caf\xe9 rules\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.register() is True
        raw = host.read_bytes()
        assert raw.startswith(b"caf\xe9 rules\n")
        assert raw.endswith(_LINE.encode() + b"\n")
        assert imp.is_registered() is True

    def test_prune_preserves_invalid_utf8_bytes(self, tmp_path: Path) -> None:
        host = tmp_path / "CLAUDE.md"
        host.write_bytes(b"caf\xe9\n" + _LINE.encode() + b"\ntail\xff\n")
        imp = ClaudeMdImport(host, _LINE)

        assert imp.prune() is True
        assert host.read_bytes() == b"caf\xe9\ntail\xff\n"
