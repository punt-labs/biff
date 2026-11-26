"""Places a private key rests: the OS keyring, an encrypted file, the macOS keychain."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_HEX_RE = re.compile(r"[0-9a-f]{1,128}")
_SCRYPT_N = 2**15
_KEYCHAIN_NOT_FOUND = 44


class VaultError(Exception):
    """A vault could not store or release a secret."""


class SecretVault(Protocol):
    """Somewhere a secret is kept, addressed by service and account."""

    def get(self, service: str, account: str) -> str | None:
        """Return the secret, or ``None`` when none is stored."""
        ...

    def has(self, service: str, account: str) -> bool:
        """Return whether a secret is stored, without releasing it."""
        ...

    def put(self, service: str, account: str, secret: str) -> None:
        """Store *secret*, replacing any earlier one."""
        ...


class KeyringModule(Protocol):
    """The two functions of the ``keyring`` module that biff calls."""

    def get_password(self, service: str, account: str) -> str | None:
        """Return the stored password or ``None``."""
        ...

    def set_password(self, service: str, account: str, password: str) -> None:
        """Store a password."""
        ...


def _check_name(kind: str, value: str) -> None:
    """Reject names that could escape a directory or a keychain query."""
    if not _NAME_RE.fullmatch(value):
        msg = f"invalid {kind} {value!r}: use lowercase letters, digits, - and _"
        raise VaultError(msg)


@final
class KeyringVault:
    """The OS keyring: macOS Keychain or the Linux Secret Service."""

    __slots__ = ("_keyring",)

    _keyring: KeyringModule

    def __new__(cls, keyring: KeyringModule) -> KeyringVault:
        """Wrap *keyring*, the ``keyring`` module or a stand-in."""
        self = super().__new__(cls)
        self._keyring = keyring
        return self

    def get(self, service: str, account: str) -> str | None:
        """Return the stored secret, or ``None`` when absent."""
        return self._keyring.get_password(service, account)

    def has(self, service: str, account: str) -> bool:
        """Return whether the keyring holds the secret."""
        return self._keyring.get_password(service, account) is not None

    def put(self, service: str, account: str, secret: str) -> None:
        """Store *secret* in the keyring."""
        self._keyring.set_password(service, account, secret)


@final
class PassphraseFileVault:
    """Secrets in scrypt + AES-GCM files, unlocked by a passphrase on every read.

    For a headless Linux machine with no keyring, and for the owner key on
    Linux, where the Secret Service releases a secret to any process of the
    logged-in user once it is unlocked.  The passphrase is asked for each
    time and is never read from the environment, where an agent could.
    """

    __slots__ = ("_ask", "_directory")

    _directory: Path
    _ask: Callable[[str], str]

    def __new__(cls, directory: Path, ask: Callable[[str], str]) -> PassphraseFileVault:
        """Keep files in *directory*; *ask* prompts and returns a passphrase."""
        self = super().__new__(cls)
        self._directory = directory
        self._ask = ask
        return self

    def get(self, service: str, account: str) -> str | None:
        """Decrypt the secret after asking for the passphrase."""
        path = self._path(service, account)
        if not path.exists():
            return None
        blob: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
        salt, nonce, cipher = (
            base64.b64decode(blob[k]) for k in ("salt", "nonce", "ct")
        )
        key = self._derive(self._ask(f"passphrase for {service}/{account}: "), salt)
        try:
            plain = AESGCM(key).decrypt(nonce, cipher, f"{service}:{account}".encode())
        except InvalidTag as exc:
            msg = f"wrong passphrase for {service}/{account}"
            raise VaultError(msg) from exc
        return plain.decode()

    def has(self, service: str, account: str) -> bool:
        """Return whether the encrypted file exists; no passphrase is asked."""
        return self._path(service, account).exists()

    def put(self, service: str, account: str, secret: str) -> None:
        """Encrypt *secret* under a new passphrase and write it, mode 0600."""
        path = self._path(service, account)
        salt, nonce = os.urandom(16), os.urandom(12)
        key = self._derive(self._ask(f"new passphrase for {service}/{account}: "), salt)
        cipher = AESGCM(key).encrypt(
            nonce, secret.encode(), f"{service}:{account}".encode()
        )
        blob = {
            name: base64.b64encode(raw).decode()
            for name, raw in (("salt", salt), ("nonce", nonce), ("ct", cipher))
        }
        self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(blob, handle)

    def _path(self, service: str, account: str) -> Path:
        _check_name("service", service)
        _check_name("account", account)
        return self._directory / f"{service}.{account}.enc"

    @staticmethod
    def _derive(passphrase: str, salt: bytes) -> bytes:
        return Scrypt(salt=salt, length=32, n=_SCRYPT_N, r=8, p=1).derive(
            passphrase.encode()
        )


def run_security(
    args: list[str], stdin: str | None = None, timeout: float | None = None
) -> str:
    """Run macOS ``security`` with *args*, feeding it *stdin*; return its output."""
    done = subprocess.run(
        ["/usr/bin/security", *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=timeout,
    )
    return done.stdout


@final
class MacKeychainVault:
    """A macOS keychain item that trusts no application.

    ``-T ""`` leaves the item's access list empty, so every read, by any
    process including biff itself, makes macOS ask the person at the screen
    to allow it.  A process that cannot click the dialog cannot read the key.
    The secret goes to ``security -i`` on its standard input, never on a
    command line, where any process could read it from the process table.
    """

    __slots__ = ("_keychain", "_run")

    _run: Callable[[list[str], str | None], str]
    _keychain: tuple[str, ...]

    def __new__(
        cls,
        run: Callable[[list[str], str | None], str] = run_security,
        keychain: Path | None = None,
    ) -> MacKeychainVault:
        """Use *run* to invoke ``security``, in *keychain* when given.

        The default keychain is the user's login keychain.  A recording
        stand-in for *run* serves unit tests; a throwaway *keychain* serves
        the manual presence test.
        """
        self = super().__new__(cls)
        self._run = run
        self._keychain = (str(keychain),) if keychain else ()
        return self

    def get(self, service: str, account: str) -> str | None:
        """Read the secret; macOS asks the user to allow it."""
        _check_name("service", service)
        _check_name("account", account)
        try:
            out = self._run(
                [
                    "find-generic-password",
                    "-s",
                    service,
                    "-a",
                    account,
                    "-w",
                    *self._keychain,
                ],
                None,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == _KEYCHAIN_NOT_FOUND:
                return None
            msg = f"keychain read of {service}/{account} failed (exit {exc.returncode})"
            raise VaultError(msg) from exc
        return out.strip()

    @staticmethod
    def _quote(word: str) -> str:
        """Double-quote *word* for one line of ``security -i`` input."""
        return '"' + word.replace("\\", "\\\\").replace('"', '\\"') + '"'

    def has(self, service: str, account: str) -> bool:
        """Return whether the item exists; attributes are read, not the secret."""
        _check_name("service", service)
        _check_name("account", account)
        try:
            self._run(
                [
                    "find-generic-password",
                    "-s",
                    service,
                    "-a",
                    account,
                    *self._keychain,
                ],
                None,
            )
        except subprocess.CalledProcessError as exc:
            if exc.returncode == _KEYCHAIN_NOT_FOUND:
                return False
            msg = f"keychain lookup of {service}/{account} failed"
            raise VaultError(msg) from exc
        return True

    def put(self, service: str, account: str, secret: str) -> None:
        """Create or replace the item with an empty access list."""
        _check_name("service", service)
        _check_name("account", account)
        if not _HEX_RE.fullmatch(secret):
            msg = "keychain secret must be hex, so it needs no shell quoting"
            raise VaultError(msg)
        line = (
            f"add-generic-password -U -s {service} -a {account} "
            f'-T "" -w {secret}'
            + "".join(f" {self._quote(path)}" for path in self._keychain)
            + "\n"
        )
        try:
            self._run(["-i"], line)
        except subprocess.CalledProcessError as exc:
            msg = (
                f"keychain write of {service}/{account} failed (exit {exc.returncode})"
            )
            raise VaultError(msg) from exc
        if not self.has(service, account):
            # ``security -i`` reports a rejected command on stdout, not by exit code.
            msg = f"keychain write of {service}/{account} did not create the item"
            raise VaultError(msg)
