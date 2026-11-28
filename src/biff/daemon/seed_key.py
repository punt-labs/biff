"""The random key that keeps a token out of reach of what biffd stores."""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import secrets
import stat
from collections.abc import Generator
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

        A file that is empty, short, a directory, a link, another user's, open to
        others or unreadable is moved aside, with a warning: biffd keeps serving and
        the records it holds stay dormant, because the hashes made with the old key no
        longer match.  A file is never removed when it is merely missing, and a key
        another biffd linked into place first is the one used, so two starts at once
        end with the key on disk.

        An exclusive lock on a lock file beside the key is held for the whole of
        it, so two biffd starting at once take turns and cannot move each other's
        good key aside.

        Raises:
            OSError: The directory cannot be written.
        """
        with self._locked():
            return self._load()

    @contextlib.contextmanager
    def _locked(self) -> Generator[None]:
        """Hold an exclusive lock on the lock file beside the key."""
        fd = os.open(
            self._path.with_name(f"{self._path.name}.lock"),
            os.O_RDWR | os.O_CREAT,
            _PRIVATE,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> bytes:
        key = self._read()
        if key is not None:
            return key
        fresh = secrets.token_bytes(_BYTES)
        self._create(fresh)
        key = self._read() or fresh
        if key == fresh and self._path.with_name(_RECORD).exists():
            logger.warning(
                "made a new key at %s beside an existing %s; sessions opened with "
                "the old key are kept dormant, and a changed 'credentials or path' "
                "note does not mean another account",
                self._path,
                _RECORD,
            )
        return key

    def _read(self) -> bytes | None:
        """Return the key if the file is usable; ``None`` if missing or moved aside."""
        try:
            info = self._path.lstat()
        except FileNotFoundError:
            return None
        except OSError:
            self._set_aside("cannot be examined")
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
            self._set_aside(why, info.st_mode)
            return None
        try:
            return self._path.read_bytes()
        except OSError:
            self._set_aside("cannot be read", info.st_mode)
            return None

    def _set_aside(self, why: str, mode: int = 0) -> None:
        """Move the unusable file to the one ``.bad`` name, private, and say so.

        It is replaced by the next one, so they do not pile up, and a file that was
        open to others is made private: the old key material must not stay readable.
        """
        aside = self._path.with_name(f"{self._path.name}.bad")
        logger.warning(
            "the key file %s %s; moving it aside and making a new one", self._path, why
        )
        try:
            self._path.replace(aside)
            if stat.S_ISREG(mode):
                aside.chmod(_PRIVATE)
        except OSError:
            logger.warning("the key file %s could not be moved aside", self._path)

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
