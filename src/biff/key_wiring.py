"""Choose where this machine keeps the owner key and the agent keys."""

from __future__ import annotations

import getpass
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, Self, final

import keyring
import keyring.backends.fail
import keyring.errors

from biff.key_vault import KeyringModule, KeyringVault, SecretVault, VaultError
from biff.keychain_vault import MacKeychainVault
from biff.passphrase_vault import PassphraseFileVault

logger = logging.getLogger(__name__)

_TTY = Path("/dev/tty")


class KeyringProbe(KeyringModule, Protocol):
    """The keyring module, plus a check that a real backend is present."""

    def is_usable(self) -> bool:
        """Return whether the keyring has a working backend."""
        ...


class KeyringPackage(KeyringModule, Protocol):
    """The ``keyring`` package: its two password calls and its backend lookup."""

    def get_keyring(self) -> object:
        """Return the backend ``keyring`` has chosen."""
        ...


@final
class SystemKeyring:
    """The installed ``keyring`` package behind :class:`KeyringProbe`."""

    __slots__ = ("_package",)

    _package: KeyringPackage

    def __new__(cls, package: KeyringPackage = keyring) -> Self:
        """Wrap *package*, the ``keyring`` module unless a test supplies one."""
        self = super().__new__(cls)
        self._package = package
        return self

    def is_usable(self) -> bool:
        """Return whether a Keychain or Secret Service backend is installed.

        Only "no backend at all" answers ``False``: the fail backend that
        ``keyring`` falls back to on a headless Linux machine.  Any error while
        finding out is a ``VaultError``, never a reason to start a second key
        store beside a keyring that may already hold keys.
        """
        try:
            backend = self._package.get_keyring()
        except (keyring.errors.KeyringError, RuntimeError) as exc:
            msg = f"cannot determine the keyring backend: {exc}"
            raise VaultError(msg) from exc
        return not isinstance(backend, keyring.backends.fail.Keyring)

    def get_password(self, service: str, account: str) -> str | None:
        """Return the stored password or ``None``."""
        return self._package.get_password(service, account)

    def set_password(self, service: str, account: str, password: str) -> None:
        """Store a password."""
        self._package.set_password(service, account, password)


@final
class TtyPassphrase:
    """Reads a passphrase from the controlling terminal and nowhere else.

    ``getpass`` falls back to standard input when it cannot open the terminal,
    and standard input is the one stream a script or an agent can feed.  So
    the terminal is opened first, and an unavailable one is an error.
    """

    __slots__ = ("_reader", "_tty")

    _tty: Path
    _reader: Callable[[str], str]

    def __new__(
        cls,
        tty_path: Path = _TTY,
        reader: Callable[[str], str] = getpass.getpass,
    ) -> Self:
        """Prompt through *reader* once *tty_path* has opened."""
        self = super().__new__(cls)
        self._tty = tty_path
        self._reader = reader
        return self

    def __call__(self, prompt: str) -> str:
        """Return the passphrase typed at *prompt*."""
        try:
            os.close(os.open(self._tty, os.O_RDWR))
        except OSError as exc:
            msg = f"cannot open /dev/tty ({exc}); run this from a terminal"
            raise VaultError(msg) from exc
        return self._reader(prompt)


DEFAULT_ASK = TtyPassphrase()

_KEYRING_BACKEND = "keyring"
_FILE_BACKEND = "file"


@final
class BackendRecord:
    """Which backend holds this machine's agent keys, remembered across runs.

    Without it, a login session that cannot reach the Secret Service would
    quietly select the encrypted-file vault, find no key there and mint a
    second one, and ``biff key agent`` would publish it over the first.
    """

    __slots__ = ("_path",)

    _path: Path

    def __new__(cls, path: Path) -> Self:
        """Keep the record in the file at *path*."""
        self = super().__new__(cls)
        self._path = path
        return self

    def read(self) -> str | None:
        """Return the recorded backend, or ``None`` before any key command ran."""
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read {self._path}: {exc}"
            raise VaultError(msg) from exc
        if text not in {_KEYRING_BACKEND, _FILE_BACKEND}:
            msg = (
                f"{self._path} holds {text!r}, not 'keyring' or 'file'; "
                "fix it to name the backend that holds your agent keys"
            )
            raise VaultError(msg)
        return text

    def write(self, backend: str) -> None:
        """Record *backend* for every later run."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self._path.write_text(backend + "\n", encoding="utf-8")
        except OSError as exc:
            msg = f"cannot record the agent key backend in {self._path}: {exc}"
            raise VaultError(msg) from exc


def machine_vaults(
    home: Path,
    ask: Callable[[str], str] | None = None,
    *,
    platform: str | None = None,
    backend: KeyringProbe | None = None,
) -> tuple[SecretVault, SecretVault]:
    """Return ``(owner_vault, agent_vault)`` for this machine.

    macOS keeps the owner key in a keychain item that trusts no application,
    so each read needs the person to click.  Elsewhere the owner key sits in
    a passphrase-encrypted file: the Secret Service releases its secrets to
    any process of the logged-in user, which is the threat.  Agent keys use
    the keyring when a backend exists and the encrypted file only when none
    does; the vault in use is named in the output of ``biff key agent``.  The
    choice is made once per machine and recorded: a keyring that is
    unavailable later is an error, never a reason to start a second key store.
    """
    ask = ask or DEFAULT_ASK
    probe = backend or SystemKeyring()
    keys = home / ".punt-labs" / "biff" / "keys"
    owner: SecretVault = (
        MacKeychainVault()
        if (platform or sys.platform) == "darwin"
        else PassphraseFileVault(keys / "owner", ask)
    )
    is_usable = probe.is_usable()
    record = BackendRecord(keys / "agent-backend")
    chosen = record.read()
    if chosen is None:
        chosen = _KEYRING_BACKEND if is_usable else _FILE_BACKEND
        record.write(chosen)
    if chosen == _KEYRING_BACKEND:
        if not is_usable:
            msg = (
                "agent keys on this machine live in the OS keyring, which is not "
                "available in this session; unlock it or run from a session that "
                "has it, so no second key is made"
            )
            raise VaultError(msg)
        return owner, KeyringVault(probe)
    if is_usable:
        logger.info("agent keys stay in an encrypted file; the keyring is unused")
    else:
        logger.warning("no keyring backend; agent keys will rest in an encrypted file")
    return owner, PassphraseFileVault(keys / "agent", ask)
