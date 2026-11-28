"""The relay variables biffd was last started with, kept in its run directory."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Self, final

from biff.relay_seed import RelaySeed

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SeedRecord"]

logger = logging.getLogger(__name__)

_PRIVATE: int = 0o600


@final
class SeedRecord:
    """Notices that biffd came back with other relay settings than it last had.

    A restart under a service manager has none of the environment a client gave
    the first biffd, and every session opened under that environment is then on
    a relay this biffd does not use.  The file holds variable names and the relay's
    host and port, never a value or a digest.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        """Keep the record at *path*, in a directory that already exists."""
        self = super().__new__(cls)
        self._path = path
        return self

    def record(self, seed: RelaySeed) -> str:
        """Write *seed*'s names; say what changed since the last start, if anything.

        The first start has nothing to compare with and says nothing.  A file that
        cannot be written is logged, not raised: biffd serves without the note.
        """
        names = seed.identity()
        try:
            previous = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            previous = ""
        except OSError:
            logger.warning("could not read %s", self._path, exc_info=True)
            previous = ""
        try:
            self._write(names)
        except OSError:
            logger.warning("could not write %s", self._path, exc_info=True)
        if previous:
            return RelaySeed.explain_change(previous, names)
        return ""

    def _write(self, names: str) -> None:
        temp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(f"{names}\n")
        temp.replace(self._path)
