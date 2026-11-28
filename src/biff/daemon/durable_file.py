"""Replace a small private file so that a crash leaves the old one or the new one."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import final

__all__ = ["DurableFile"]


@final
class DurableFile:
    """Writes a file whole, synced, under a private directory."""

    @staticmethod
    def replace(directory: Path, name: str, text: str) -> None:
        """Make *directory*/*name* hold *text*: data synced, then name, then directory.

        The file is mode 0600 and the directory 0700 when this creates it.
        """
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".durable-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            Path(tmp).chmod(0o600)
            Path(tmp).replace(directory / name)
            DurableFile._sync_directory(directory)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
