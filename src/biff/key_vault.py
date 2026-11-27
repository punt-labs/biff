"""The vault interface, its error, and the OS keyring vault."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol, final

from keyring.errors import KeyringError

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class VaultError(Exception):
    """A vault could not store or release a secret."""


class CorruptKeyError(VaultError):
    """A vault released a value that is not a key: damaged or tampered with."""


@final
@dataclass(frozen=True, slots=True)
class VaultKey:
    """The address of one secret: a service and an account.

    Both names reach a file name or a keychain query, so a name that could
    escape either is refused when the key is built, not when a vault gets
    around to it.
    """

    service: str
    account: str

    def __post_init__(self) -> None:
        """Reject names that could escape a directory or a keychain query."""
        for kind, value in (("service", self.service), ("account", self.account)):
            if not _NAME_RE.fullmatch(value):
                msg = f"invalid {kind} {value!r}: use lowercase letters, digits, - _"
                raise VaultError(msg)

    @property
    def where(self) -> str:
        """Name the secret for a message to the user."""
        return f"{self.service}/{self.account}"


class SecretVault(Protocol):
    """Somewhere a secret is kept, addressed by a :class:`VaultKey`."""

    @property
    def label(self) -> str:
        """A phrase naming where secrets rest, for a message to the user."""
        ...

    def get(self, key: VaultKey) -> str | None:
        """Return the secret, or ``None`` when none is stored."""
        ...

    def has(self, key: VaultKey) -> bool:
        """Return whether a secret is stored, without releasing it."""
        ...

    def put(self, key: VaultKey, secret: str) -> None:
        """Store *secret*, replacing any earlier one."""
        ...


class KeyringModule(Protocol):
    """The two functions of the ``keyring`` module that biff calls."""

    def get_password(self, service: str, account: str, /) -> str | None:
        """Return the stored password or ``None``."""
        ...

    def set_password(self, service: str, account: str, password: str, /) -> None:
        """Store a password."""
        ...


@final
class KeyringVault:
    """The OS keyring: macOS Keychain or the Linux Secret Service."""

    __slots__ = ("_keyring",)

    label = "the OS keyring"

    _keyring: KeyringModule

    def __new__(cls, keyring: KeyringModule) -> KeyringVault:
        """Wrap *keyring*, the ``keyring`` module or a stand-in."""
        self = super().__new__(cls)
        self._keyring = keyring
        return self

    def get(self, key: VaultKey) -> str | None:
        """Return the stored secret, or ``None`` when absent."""
        try:
            return self._keyring.get_password(key.service, key.account)
        except KeyringError as exc:
            msg = f"keyring read of {key.where} failed: {exc}"
            raise VaultError(msg) from exc

    def has(self, key: VaultKey) -> bool:
        """Return whether the keyring holds the secret.

        A keyring that cannot answer raises: reading it as "absent" would let
        a caller mint a second key beside the one it could not see.
        """
        try:
            return self._keyring.get_password(key.service, key.account) is not None
        except KeyringError as exc:
            msg = f"keyring read of {key.where} failed: {exc}"
            raise VaultError(msg) from exc

    def put(self, key: VaultKey, secret: str) -> None:
        """Store *secret* in the keyring."""
        try:
            self._keyring.set_password(key.service, key.account, secret)
        except KeyringError as exc:
            msg = f"keyring write of {key.where} failed: {exc}"
            raise VaultError(msg) from exc
