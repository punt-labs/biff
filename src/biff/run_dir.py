"""The biffd run directory: bearer token and port file."""

from __future__ import annotations

import os
import re
import secrets
import stat
from pathlib import Path
from typing import Self, final

__all__ = ["DEFAULT_PORT", "RunDir"]

DEFAULT_PORT = 8424
"""Loopback port of the daemon; the next free one in the 8420 to 8430 block."""

_TOKEN_BYTES = 32
_TOKEN_LENGTH = 43  # token_urlsafe(32): ceil(32 * 4 / 3) characters
_TOKEN_SHAPE = re.compile(rf"[A-Za-z0-9_-]{{{_TOKEN_LENGTH}}}")


@final
class RunDir:
    """Locates and guards ``~/.punt-labs/biff/run/``.

    The daemon writes the token and port file; every client reads them.  The
    path is injectable so tests never touch the real home directory.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        """Create the run directory handle for *path*."""
        self = super().__new__(cls)
        self._path = path
        return self

    @classmethod
    def default(cls) -> Self:
        """Return the run directory under the current user's home tree."""
        return cls(Path.home() / ".punt-labs" / "biff" / "run")

    @property
    def path(self) -> Path:
        """The directory itself."""
        return self._path

    @property
    def token_path(self) -> Path:
        """The bearer token file."""
        return self._path / "serve.token"

    @property
    def port_path(self) -> Path:
        """The file naming the port a running daemon listens on."""
        return self._path / "serve.port"

    def ensure(self) -> None:
        """Create the directory private to the user, or refuse one that is not.

        Raises:
            PermissionError: The directory exists but another user could read
                or replace what is in it.
        """
        self._path.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._require_private(self._path, 0o700, "chmod 700")

    def ensure_token(self) -> str:
        """Return the token, creating it once at mode 0600.

        The token is written to a temporary file in the run directory, flushed to
        disk, and only then hard-linked into place.  ``link`` fails if the name
        exists, so exactly one creator wins; the file appears complete or not at
        all, never empty.  A creator that loses the race reads the winner's file.

        Raises:
            PermissionError: The run directory or an existing token file is not
                private to the current user.
            ValueError: An existing token file does not hold a token.
        """
        self.ensure()
        if self.token_path.exists():
            return self.read_token()
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        temp = self._write_private_temp(self.token_path, token)
        try:
            os.link(temp, self.token_path)
        except FileExistsError:
            return self.read_token()
        finally:
            temp.unlink(missing_ok=True)
        return token

    def read_token(self) -> str:
        """Return the stored token.

        Raises:
            FileNotFoundError: No daemon has created the token.
            PermissionError: The directory or the file is not private to the
                current user, so the token cannot be trusted.
            ValueError: The file does not hold a token of the length and alphabet
                biffd creates; an empty or short one would make every MAC
                computable by anyone.
        """
        path = self.token_path
        if not path.exists():
            msg = f"no token at {path}: biffd has never run here"
            raise FileNotFoundError(msg)
        self._require_private(self._path, 0o700, "chmod 700")
        self._require_private(path, 0o600, "chmod 600")
        token = path.read_text().strip()
        if not _TOKEN_SHAPE.fullmatch(token):
            msg = (
                f"{path} does not hold a biffd token ({len(token)} characters, "
                f"expected {_TOKEN_LENGTH} of A-Z a-z 0-9 - _); remove it and "
                "start biffd again to create a new one"
            )
            raise ValueError(msg)
        return token

    def write_port(self, port: int) -> None:
        """Record the port the running daemon listens on, replacing the file whole."""
        self.ensure()
        temp = self._write_private_temp(self.port_path, f"{port}\n")
        temp.replace(self.port_path)

    def read_port(self) -> int:
        """Return the recorded port, or the default when no daemon recorded one.

        Raises:
            ValueError: The port file does not hold a port number.
        """
        try:
            text = self.port_path.read_text().strip()
        except FileNotFoundError:
            return DEFAULT_PORT
        try:
            return int(text)
        except ValueError as exc:
            msg = f"{self.port_path} holds {text!r}, not a port number"
            raise ValueError(msg) from exc

    def remove_port(self, *, owned_by: int | None = None) -> None:
        """Remove the port file; a daemon that never wrote it is not an error.

        With *owned_by*, remove it only while it still names that port, so a
        daemon that stops late does not erase the record of the one that replaced
        it.
        """
        if owned_by is not None:
            try:
                recorded = self.read_port() if self.port_path.exists() else None
            except ValueError:
                recorded = None
            if recorded != owned_by:
                return
        self.port_path.unlink(missing_ok=True)

    @staticmethod
    def _write_private_temp(target: Path, text: str) -> Path:
        """Write *text* to a flushed, private temporary file beside *target*."""
        temp = target.with_name(
            f"{target.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return temp

    @staticmethod
    def _require_private(path: Path, mode: int, repair: str) -> None:
        """Refuse *path* unless the current user owns it at exactly *mode*.

        Raises:
            PermissionError: Another user owns *path*, or it is more open than
                *mode*.
        """
        info = path.stat()
        if info.st_uid != os.getuid():
            msg = (
                f"{path} is owned by uid {info.st_uid}, not you (uid "
                f"{os.getuid()}); biffd's token cannot be trusted there"
            )
            raise PermissionError(msg)
        actual = stat.S_IMODE(info.st_mode)
        if actual & ~mode:
            msg = (
                f"{path} has mode {actual:04o}; it must be {mode:04o}. "
                f"Run: {repair} {path}"
            )
            raise PermissionError(msg)
