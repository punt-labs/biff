"""The random key that keeps a token out of reach of what biffd stores."""

from __future__ import annotations

import contextlib
import logging
import os
import secrets
import stat
from typing import TYPE_CHECKING, Final, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SeedKey"]

logger = logging.getLogger(__name__)

_BYTES: Final = 32
_PRIVATE: Final = 0o600
_RECORD: Final = "relay-seed"


@final
class SeedKey:
    """A 32-byte key in a private file of biffd's run directory.

    It keys the hash of the relay settings in the records biffd keeps, so a
    low-entropy token cannot be guessed offline from them.  The hash protects copies
    of those records that leave the run directory; whoever can read the records
    beside the key can read the key too.  It is made once and kept.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        """Keep the key at *path*, in a directory that already exists."""
        self = super().__new__(cls)
        self._path = path
        return self

    def load(self) -> bytes:
        """Return the key, making it on first use and again when it is unusable.

        A file that is empty, short, a link, another user's or open to others is
        replaced, with a warning: biffd keeps serving and the records it holds stay
        dormant, because the hashes made with the old key no longer match.

        Raises:
            OSError: The directory cannot be written.
        """
        key = self._read()
        if key is not None:
            return key
        self._path.unlink(missing_ok=True)
        key = secrets.token_bytes(_BYTES)
        self._create(key)
        key = self._read() or key  # another biffd may have made it first
        if self._path.with_name(_RECORD).exists():
            logger.warning(
                "made a new key at %s beside an existing %s; sessions opened with "
                "the old key are kept dormant, and a changed 'credentials or path' "
                "note does not mean another account",
                self._path,
                _RECORD,
            )
        return key

    def _read(self) -> bytes | None:
        """Return the key if the file is usable, else say why not and return None."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return None
        why = ""
        if not stat.S_ISREG(info.st_mode):
            why = "is not a regular file"
        elif info.st_uid != os.getuid():
            why = "belongs to another user"
        elif info.st_mode & 0o077:
            why = "is open to other users"
        elif info.st_size != _BYTES:
            why = f"is not {_BYTES} bytes"
        if why:
            logger.warning("the key file %s %s; making a new one", self._path, why)
            return None
        try:
            return self._path.read_bytes()
        except OSError:
            logger.warning(
                "the key file %s cannot be read; making a new one", self._path
            )
            return None

    def _create(self, key: bytes) -> None:
        """Write *key* to a flushed private temporary file, then link it into place."""
        temp = self._path.with_name(f"{self._path.name}.{os.getpid()}.tmp")
        temp.unlink(missing_ok=True)
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE)
        try:
            with os.fdopen(fd, "wb") as out:
                os.fchmod(out.fileno(), _PRIVATE)
                out.write(key)
                out.flush()
                os.fsync(out.fileno())
            # A biffd that made the key first wins; the next read takes theirs.
            with contextlib.suppress(FileExistsError):
                os.link(temp, self._path)
        finally:
            temp.unlink(missing_ok=True)
