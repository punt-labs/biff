"""Secrets in scrypt + AES-GCM files, unlocked by a passphrase on every read."""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from biff.key_vault import VaultError, check_vault_name

_SCRYPT_N = 2**17
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MIN_N = 2**14
_SCRYPT_MAX_N = 2**20
_SCRYPT_MAX_R = 16
_SCRYPT_MAX_P = 4
_FILE_VERSION = 1
_MIN_PASSPHRASE = 12


@final
@dataclass(frozen=True, slots=True)
class _SealedFile:
    """A key file: scrypt parameters and the salt, nonce and ciphertext they seal."""

    n: int
    r: int
    p: int
    salt: bytes
    nonce: bytes
    ciphertext: bytes

    def has_supported_cost(self) -> bool:
        """Return whether the parameters are ones this vault will run.

        A file is read under the parameters it records, so the bounds stop a
        tampered file from demanding gigabytes of memory.
        """
        return (
            _SCRYPT_MIN_N <= self.n <= _SCRYPT_MAX_N
            and not self.n & (self.n - 1)
            and 1 <= self.r <= _SCRYPT_MAX_R
            and 1 <= self.p <= _SCRYPT_MAX_P
        )

    def to_json(self) -> str:
        """Serialise the file, versioned, with every parameter spelled out."""
        return json.dumps(
            {
                "v": _FILE_VERSION,
                "n": self.n,
                "r": self.r,
                "p": self.p,
                "salt": base64.b64encode(self.salt).decode(),
                "nonce": base64.b64encode(self.nonce).decode(),
                "ct": base64.b64encode(self.ciphertext).decode(),
            }
        )


@final
class PassphraseFileVault:
    """Secrets in scrypt + AES-GCM files, unlocked by a passphrase on every read.

    For a headless Linux machine with no keyring, and for the owner key on
    Linux, where the Secret Service releases a secret to any process of the
    logged-in user once it is unlocked.  The passphrase is asked for each
    time and is never read from the environment, where an agent could.  Each
    file records its own scrypt parameters, so raising the cost later leaves
    existing files readable.
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

    @property
    def label(self) -> str:
        """Name this vault for a message to the user."""
        return f"an encrypted file in {self._directory}"

    def get(self, service: str, account: str) -> str | None:
        """Decrypt the secret after asking for the passphrase."""
        path = self._path(service, account)
        where = f"{service}/{account}"
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            msg = f"cannot read the key file for {where}: {exc}"
            raise VaultError(msg) from exc
        blob = self._parse(text, where)
        key = self._derive(self._prompt(f"passphrase for {where}: ", where), blob)
        try:
            plain = AESGCM(key).decrypt(
                blob.nonce, blob.ciphertext, f"{service}:{account}".encode()
            )
        except InvalidTag as exc:
            msg = f"wrong passphrase for {where}"
            raise VaultError(msg) from exc
        return plain.decode()

    def has(self, service: str, account: str) -> bool:
        """Return whether the encrypted file exists; no passphrase is asked."""
        return self._path(service, account).exists()

    def put(self, service: str, account: str, secret: str) -> None:
        """Encrypt *secret* under a confirmed passphrase and write it, mode 0600.

        The file is written beside the live one and renamed over it, so a
        failure never leaves a truncated key behind.
        """
        path = self._path(service, account)
        where = f"{service}/{account}"
        passphrase = self._prompt(f"new passphrase for {where}: ", where)
        if len(passphrase) < _MIN_PASSPHRASE:
            msg = f"the passphrase must be at least {_MIN_PASSPHRASE} characters"
            raise VaultError(msg)
        if self._prompt(f"confirm passphrase for {where}: ", where) != passphrase:
            msg = "the passphrases do not match"
            raise VaultError(msg)
        salt, nonce = os.urandom(16), os.urandom(12)
        blank = _SealedFile(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, salt, nonce, b"")
        key = self._derive(passphrase, blank)
        cipher = AESGCM(key).encrypt(
            nonce, secret.encode(), f"{service}:{account}".encode()
        )
        sealed = _SealedFile(_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, salt, nonce, cipher)
        self._write_atomically(path, sealed.to_json(), where)

    def _prompt(self, prompt: str, where: str) -> str:
        try:
            return self._ask(prompt)
        except EOFError as exc:
            msg = f"could not read the passphrase for {where}: end of input"
            raise VaultError(msg) from exc

    def _write_atomically(self, path: Path, text: str, where: str) -> None:
        tmp = ""
        try:
            self._directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, tmp = tempfile.mkstemp(
                dir=self._directory, prefix=".key-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            Path(tmp).replace(path)
        except OSError as exc:
            if tmp:
                Path(tmp).unlink(missing_ok=True)
            msg = f"could not write the key file for {where}: {exc}"
            raise VaultError(msg) from exc

    def _path(self, service: str, account: str) -> Path:
        check_vault_name("service", service)
        check_vault_name("account", account)
        return self._directory / f"{service}.{account}.enc"

    @staticmethod
    def _parse(text: str, where: str) -> _SealedFile:
        """Return the parameters and byte fields of a key file, or raise."""
        try:
            raw = json.loads(text)
            if raw["v"] != _FILE_VERSION:
                msg = f"unsupported key file version {raw['v']!r} for {where}"
                raise VaultError(msg)
            sealed = _SealedFile(
                n=int(raw["n"]),
                r=int(raw["r"]),
                p=int(raw["p"]),
                salt=base64.b64decode(raw["salt"], validate=True),
                nonce=base64.b64decode(raw["nonce"], validate=True),
                ciphertext=base64.b64decode(raw["ct"], validate=True),
            )
        except (ValueError, KeyError, TypeError, binascii.Error) as exc:
            msg = f"the key file for {where} is corrupt: {exc!r}"
            raise VaultError(msg) from exc
        if not sealed.has_supported_cost():
            msg = f"the key file for {where} has unsupported scrypt parameters"
            raise VaultError(msg)
        return sealed

    @staticmethod
    def _derive(passphrase: str, sealed: _SealedFile) -> bytes:
        return Scrypt(
            salt=sealed.salt, length=32, n=sealed.n, r=sealed.r, p=sealed.p
        ).derive(passphrase.encode())
