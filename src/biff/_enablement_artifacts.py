"""Checkout artifact writes and fixed-label enablement diagnostics."""

from __future__ import annotations

import errno
import importlib.resources
import logging
import os
import stat
from dataclasses import dataclass
from typing import TYPE_CHECKING

from biff._stdlib import (
    enabled_marker_path,
    remove_enabled_marker,
    write_enabled_marker,
)

if TYPE_CHECKING:
    from pathlib import Path

_logger = logging.getLogger("biff.enablement")
_LOCK_IGNORE = "*.punt-import.lock"


@dataclass(frozen=True, slots=True)
class CheckoutArtifacts:
    """Own the ancillary guide and ignore artifacts for one checkout."""

    _root: Path

    def write_marker(self) -> bool:
        """Write the policy marker, reporting whether it was absent."""
        changed = not enabled_marker_path(self._root).exists()
        write_enabled_marker(self._root)
        return changed

    def remove_marker(self) -> bool:
        """Remove the policy marker, reporting whether it was present."""
        changed = enabled_marker_path(self._root).exists()
        remove_enabled_marker(self._root)
        return changed

    def ignore_locks(self) -> bool:
        """Append the local lock glob once, without following symlinks."""
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self._root / ".gitignore", flags, 0o644)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                return False
            raise
        with os.fdopen(fd, "r+b") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                return False
            text = handle.read().decode("utf-8", errors="replace")
            if _LOCK_IGNORE in (line.strip() for line in text.splitlines()):
                return False
            sep = "\n" if text and not text.endswith("\n") else ""
            handle.write(f"{sep}{_LOCK_IGNORE}\n".encode())
        return True

    def deposit_guide(self) -> bool:
        """Write the bundled guide only when its bytes differ."""
        want = (
            importlib.resources.files("biff.data")
            .joinpath("user-claude.md")
            .read_bytes()
        )
        guide = self._root / ".punt-labs" / "biff" / "CLAUDE.md"
        if guide.is_file() and guide.read_bytes() == want:
            return False
        guide.parent.mkdir(parents=True, exist_ok=True)
        guide.write_bytes(want)
        return True

    @staticmethod
    def report(operation: str, changes: dict[str, bool]) -> None:
        """Log only caller-supplied fixed labels, not artifact contents or paths."""
        _logger.info(
            "Enablement operation=%s outcome=%s changes=%s",
            operation,
            "changed" if any(changes.values()) else "unchanged",
            ",".join(label for label, changed in changes.items() if changed) or "none",
        )
