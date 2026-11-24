"""Write a file atomically, and only when its content would change."""

from __future__ import annotations

from typing import TYPE_CHECKING

from biff.relay import atomic_write

if TYPE_CHECKING:
    from pathlib import Path


class ChangedFile:
    """An identical rewrite is skipped: it would wake whatever watches the file."""

    @staticmethod
    def write(path: Path, text: str) -> None:
        """Replace *path* with *text* atomically unless it already holds *text*."""
        if not (path.exists() and path.read_text() == text):
            atomic_write(path, text)
