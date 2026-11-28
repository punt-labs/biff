"""Replace a small private file so that a crash leaves the old one or the new one."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import final

__all__ = ["DurableFile", "UnsyncedError"]


class UnsyncedError(OSError):
    """The file was replaced, but its directory entry could not be synced.

    The new content is in place: only a crash right now could still bring the old
    one back.  Anything else that raises from :meth:`DurableFile.replace` means the
    file was not replaced.
    """


@final
class DurableFile:
    """Writes a file whole, synced, under a private directory."""

    @staticmethod
    def replace(directory: Path, name: str, text: str) -> None:
        """Make *directory*/*name* hold *text*: data synced, then name, then directory.

        The file is mode 0600 and the directory 0700 when this creates it.

        Raises:
            UnsyncedError: The file was replaced; only the directory sync failed.
            OSError: The file was not replaced.
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
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        try:
            DurableFile._sync_directory(directory)
        except OSError as exc:
            msg = f"{directory / name} replaced, directory not synced: {exc}"
            raise UnsyncedError(msg) from exc

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
