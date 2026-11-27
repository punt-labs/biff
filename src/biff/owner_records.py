"""The owner public keys recorded on this machine, kept in the clear."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Self, final

from biff.key_vault import VaultError, VaultKey

_SERVICE = "biff-owner-pub"


@final
class OwnerRecords:
    """One plain file per owner holding that owner's public key in hex.

    A public key needs no secret to read, so reading it must not ask for a
    passphrase or release anything: ``biff key show`` compares a published
    owner key with this record on a machine with no keyring and no terminal.
    The files sit beside the sealed owner secret with the same permissions
    (directory 0700, file 0600).
    """

    __slots__ = ("_directory",)

    _directory: Path

    def __new__(cls, directory: Path) -> Self:
        """Keep the records in *directory*."""
        self = super().__new__(cls)
        self._directory = directory
        return self

    @property
    def label(self) -> str:
        """Name where the records are, for a message to the user."""
        return f"the owner key records in {self._directory}"

    def has(self, handle: str) -> bool:
        """Return whether *handle*'s public key is recorded."""
        return self._path(handle).exists()

    def get(self, handle: str) -> str | None:
        """Return the recorded hex public key, or ``None`` when none is recorded."""
        path = self._path(handle)
        try:
            return path.read_text(encoding="ascii").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read the owner key record for {handle}: {exc}"
            raise VaultError(msg) from exc

    def put(self, handle: str, public_hex: str) -> None:
        """Record *public_hex* for *handle*, replacing any earlier record.

        Written beside the live file and renamed over it, so a failure never
        leaves a truncated record.
        """
        path = self._path(handle)
        tmp = ""
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(dir=self._directory, prefix=".pub-")
            with os.fdopen(fd, "w", encoding="ascii") as out:
                out.write(public_hex + "\n")
            Path(tmp).replace(path)
        except OSError as exc:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            msg = f"could not record the owner key for {handle}: {exc}"
            raise VaultError(msg) from exc

    def _path(self, handle: str) -> Path:
        key = VaultKey(_SERVICE, handle)
        return self._directory / f"{key.account}.pub"
