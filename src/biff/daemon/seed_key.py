"""The random key that keeps a token out of reach of what biffd stores."""

from __future__ import annotations

import os
import secrets
import stat
from typing import TYPE_CHECKING, Final, Self, final

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["SeedKey"]

_BYTES: Final = 32
_PRIVATE: Final = 0o600


@final
class SeedKey:
    """A 32-byte key in a private file of biffd's run directory.

    It keys the hash that names the seeded token in the records biffd keeps, so a
    low-entropy token cannot be guessed offline from them.  It is made once and
    kept for the life of the run directory.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        """Keep the key at *path*, in a directory that already exists."""
        self = super().__new__(cls)
        self._path = path
        return self

    def load(self) -> bytes:
        """Return the key, making it on first use.

        Raises:
            ValueError: The file is not private or does not hold a key.
            OSError: The file cannot be read or made.
        """
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PRIVATE)
        except FileExistsError:
            return self._read()
        with os.fdopen(fd, "wb") as out:
            out.write(secrets.token_bytes(_BYTES))
        return self._read()

    def _read(self) -> bytes:
        if stat.S_IMODE(self._path.stat().st_mode) != _PRIVATE:
            msg = f"{self._path} must be readable by its owner only; chmod 600 it"
            raise ValueError(msg)
        key = self._path.read_bytes()
        if len(key) != _BYTES:
            msg = f"{self._path} does not hold a {_BYTES}-byte key; remove it"
            raise ValueError(msg)
        return key
