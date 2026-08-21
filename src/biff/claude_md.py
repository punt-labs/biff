"""Register/prune biff's user-scope ``@``-import line in ``~/.claude/CLAUDE.md``.

Per punt-kit ``tool-enable-disable.md`` §2.4-2.6: a global tool registers
exactly one *bare* ``@``-import line pointing at a file it owns entirely.
Composition happens at read time, when Claude Code resolves the import — never
at write time. This module owns only that single line; every other byte of the
user's ``CLAUDE.md`` is preserved verbatim.

It ports the atomic / symlink-resolving / byte-preserving write correctness of
vox's reference ``AtomicFile`` (``punt-labs/vox``,
``src/punt_vox/atomic_file.py``) and adds the §2.4 exclusive lock plus the
bare-line match rules (terminator-insensitive, code-block-aware). The
managed-section *marker* model that vox still carries is explicitly retired by
§2.1 / §2.11, so it is not ported.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Self, final

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["ClaudeMdImport"]

_NEW_FILE_MODE = 0o644


@final
class ClaudeMdImport:
    """Owns a single bare ``@``-import line inside a host ``CLAUDE.md``.

    The read-modify-write is serialized by an exclusive ``flock`` on a sibling
    lock file (§2.4 mandates the lock for every shared host-file mutation), and
    each write lands atomically so an interrupted write never corrupts the
    user's hand-authored file.
    """

    __slots__ = ("_host", "_import_line", "_lock_path")

    _host: Path
    _import_line: str
    _lock_path: Path

    def __new__(cls, host_path: Path, import_line: str) -> Self:
        cls._validate(import_line)
        self = super().__new__(cls)
        self._host = host_path
        self._import_line = import_line
        # Sibling lock file — a lock on the target itself would race the atomic
        # rename that replaces it, so serialize on a stable neighbour instead.
        # The name is tool-AGNOSTIC (``punt-import``, not ``biff-import``): vox,
        # quarry, and biff all mutate the same ~/.claude/CLAUDE.md (§2.6), so a
        # biff-specific lock would only exclude biff-vs-biff and a concurrent
        # `vox install` would clobber it. All punt CLIs must take this same lock.
        self._lock_path = host_path.parent / f".{host_path.name}.punt-import.lock"
        return self

    def is_registered(self) -> bool:
        """Return ``True`` when the import line is present at top level.

        A pure read — no lock needed, since every write lands atomically and a
        read therefore never observes a torn file.
        """
        return self._present(self._read())

    def register(self) -> bool:
        """Append the import line if absent. Return ``True`` if the file changed.

        Idempotent: a line already present (net of its terminator, top-level
        only) is a no-op, so re-running ``install`` never duplicates it.
        """
        with self._locked():
            text = self._read()
            if self._present(text):
                return False
            self._write(self._appended(text))
            return True

    def prune(self) -> bool:
        """Remove every top-level occurrence. Return ``True`` if the file changed.

        Collapses an accidental duplicate to zero and leaves any inert copy
        inside a code block untouched.
        """
        with self._locked():
            text = self._read()
            new_text = self._removed(text)
            if new_text == text:
                return False
            self._write(new_text)
            return True

    # ── content transforms ───────────────────────────────────────────

    def _present(self, text: str) -> bool:
        """Return ``True`` when a top-level line matches the import line."""
        lines = self._physical_lines(text)
        flags = self._top_level_flags(lines)
        return any(
            top and self._line_matches(line)
            for line, top in zip(lines, flags, strict=True)
        )

    def _appended(self, text: str) -> str:
        """Return *text* with the import line appended as one bare top-level line.

        Ensures a separating newline first (so the import is never glued to the
        user's last line) and uses the host file's existing EOL convention for
        both the separator and the appended line.
        """
        eol = self._detect_eol(text)
        result = text
        if result and not result.endswith(("\n", "\r")):
            result += eol
        return f"{result}{self._import_line}{eol}"

    def _removed(self, text: str) -> str:
        """Return *text* with every top-level occurrence of the line removed."""
        lines = self._physical_lines(text)
        flags = self._top_level_flags(lines)
        kept = [
            line
            for line, top in zip(lines, flags, strict=True)
            if not (top and self._line_matches(line))
        ]
        return "".join(kept)

    def _line_matches(self, line: str) -> bool:
        """Match a physical line against the import line, net of its terminator."""
        return line.rstrip("\r\n") == self._import_line

    @staticmethod
    def _physical_lines(text: str) -> list[str]:
        """Split into physical lines, keeping each line's terminator."""
        return text.splitlines(keepends=True)

    @staticmethod
    def _parse_fence(line: str) -> tuple[str, int] | None:
        """Return ``(marker_char, run_length)`` if *line* is a fence delimiter.

        A fence delimiter is a run of three or more of a single marker char
        (a backtick or a tilde) after up to three leading spaces. An **indented**
        line (a tab, or four or more leading spaces) is an inert indented-code
        line, never a fence delimiter (§2.4). Returns ``None`` otherwise.
        """
        bare = line.rstrip("\r\n")
        if bare.startswith("\t"):
            return None
        stripped = bare.lstrip(" ")
        if len(bare) - len(stripped) >= 4:
            return None
        if not stripped or stripped[0] not in "`~":
            return None
        marker = stripped[0]
        run = len(stripped) - len(stripped.lstrip(marker))
        return (marker, run) if run >= 3 else None

    @classmethod
    def _fenced_ranges(cls, lines: list[str]) -> list[tuple[int, int]]:
        """Return ``(open_idx, close_idx)`` index pairs of matched fenced blocks.

        A block opened by a run of *N* of a marker closes only on a later
        **same-marker** delimiter of length ``>= N`` (CommonMark); a mismatched
        marker or a shorter run is content, not a close, so an inner ``~~~`` in a
        ```` ``` ```` block never toggles the state. An unterminated opener is
        dropped (delimits nothing), so a dangling fence never swallows the rest
        of the file. Blocks do not nest — once open, every line up to the
        matching close is content.
        """
        ranges: list[tuple[int, int]] = []
        open_at: int | None = None
        open_marker = ""
        open_len = 0
        for i, line in enumerate(lines):
            fence = cls._parse_fence(line)
            if open_at is None:
                if fence is not None:
                    open_at, (open_marker, open_len) = i, fence
            elif fence is not None and fence[0] == open_marker and fence[1] >= open_len:
                ranges.append((open_at, i))
                open_at = None
        return ranges

    @classmethod
    def _top_level_flags(cls, lines: list[str]) -> list[bool]:
        """Flag each line ``True`` when it is top-level (not in a code block).

        A line is non-top-level when it lies inside a matched fenced block
        (:meth:`_fenced_ranges`) or is itself an indented code-block line — a
        tab or four or more leading spaces (§2.4). biff's own import line is
        written at column 0 with no info string, so it is top-level by
        construction unless it sits inside a genuine fenced block.
        """
        inside: set[int] = set()
        for open_idx, close_idx in cls._fenced_ranges(lines):
            # Content and the closing delimiter are inside; the opener is not.
            inside.update(range(open_idx + 1, close_idx + 1))
        flags: list[bool] = []
        for i, line in enumerate(lines):
            leading_spaces = len(line) - len(line.lstrip(" "))
            indented = line.startswith("\t") or leading_spaces >= 4
            flags.append(i not in inside and not indented)
        return flags

    @staticmethod
    def _detect_eol(text: str) -> str:
        """Return the host file's EOL convention, defaulting to ``\\n``."""
        crlf = text.find("\r\n")
        lf = text.find("\n")
        cr = text.find("\r")
        if crlf != -1 and crlf == lf - 1:
            return "\r\n"
        if lf != -1 and (cr == -1 or lf < cr):
            return "\n"
        if cr != -1:
            return "\r"
        return "\n"

    # ── I/O ────────────────────────────────────────────────────────────

    @contextmanager
    def _locked(self) -> Generator[None]:
        """Hold an exclusive lock on the sibling lock file for the whole RMW."""
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_path.open("w", encoding="utf-8") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _read(self) -> str:
        """Return the host file verbatim, or ``""`` when it does not exist.

        ``newline=""`` disables universal-newline translation so a read/write
        round-trip keeps LF, CRLF, and lone-CR endings byte-identical.
        ``errors="surrogateescape"`` makes the read byte-faithful for a host
        that is *not* valid UTF-8 (a hand-authored ``CLAUDE.md`` may hold
        latin-1 or mixed bytes): invalid bytes decode to lone surrogates and
        :meth:`_write` restores them, so a non-UTF-8 host neither crashes the
        read (``UnicodeDecodeError`` is a ``ValueError``, not ``OSError``) nor
        is corrupted on write-back.
        """
        if not self._host.is_file():
            return ""
        return self._host.read_text(
            encoding="utf-8", newline="", errors="surrogateescape"
        )

    def _write(self, text: str) -> None:
        """Replace the host file's contents with *text* atomically.

        Writes a temp file in the target's own directory, ``fsync``s it, then
        ``os.replace``s it over the target — an interrupted write leaves the
        original untouched. A symlinked path is resolved so the rename updates
        the real file and preserves the link; the existing mode is preserved.
        """
        target = self._host.resolve() if self._host.is_symlink() else self._host
        directory = target.parent
        directory.mkdir(parents=True, exist_ok=True)
        mode = (
            stat.S_IMODE(target.stat().st_mode) if target.is_file() else _NEW_FILE_MODE
        )
        fd, tmp_name = tempfile.mkstemp(
            dir=directory, prefix=f".{target.name}.", suffix=".tmp"
        )
        tmp = Path(tmp_name)
        try:
            # surrogateescape mirrors _read: a lone surrogate produced by
            # decoding an invalid host byte is written back as that exact byte.
            handle = os.fdopen(
                fd, "w", encoding="utf-8", newline="", errors="surrogateescape"
            )
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise
        try:
            with handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            # os.replace preserves the temp's 0600 mkstemp mode, so stamp the
            # intended mode before the rename or an existing 0644 file drops.
            tmp.chmod(mode)
            tmp.replace(target)
        except BaseException:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
            raise

    @staticmethod
    def _validate(import_line: str) -> None:
        """Raise ``ValueError`` unless *import_line* is a lone top-level ``@`` line.

        Validated at the construction boundary (PY-EH-1): the line is spliced
        into the host file verbatim, so a padded, multi-line, or non-``@`` value
        would inject a duplicate, a blank line, or inert markdown.
        """
        if not import_line or import_line.isspace():
            raise ValueError("import line must be non-empty")
        if "\n" in import_line or "\r" in import_line:
            raise ValueError(f"import line must be a single line: {import_line!r}")
        if import_line != import_line.strip():
            raise ValueError(
                f"import line must have no leading/trailing whitespace: {import_line!r}"
            )
        if not import_line.startswith("@"):
            raise ValueError(f"import line must begin with '@': {import_line!r}")
