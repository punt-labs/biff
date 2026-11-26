"""The vault interface, its error, and the OS keyring vault."""

from __future__ import annotations

import re
from typing import Protocol, final

from keyring.errors import KeyringError

_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")


class VaultError(Exception):
    """A vault could not store or release a secret."""


class SecretVault(Protocol):
    """Somewhere a secret is kept, addressed by service and account."""

    @property
    def label(self) -> str:
        """A phrase naming where secrets rest, for a message to the user."""
        ...

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


def check_vault_name(kind: str, value: str) -> None:
    """Reject names that could escape a directory or a keychain query."""
    if not _NAME_RE.fullmatch(value):
        msg = f"invalid {kind} {value!r}: use lowercase letters, digits, - and _"
        raise VaultError(msg)


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

    def get(self, service: str, account: str) -> str | None:
        """Return the stored secret, or ``None`` when absent."""
        try:
            return self._keyring.get_password(service, account)
        except KeyringError as exc:
            msg = f"keyring read of {service}/{account} failed: {exc}"
            raise VaultError(msg) from exc

    def has(self, service: str, account: str) -> bool:
        """Return whether the keyring holds the secret.

        A keyring that cannot answer raises: reading it as "absent" would let
        a caller mint a second key beside the one it could not see.
        """
        try:
            return self._keyring.get_password(service, account) is not None
        except KeyringError as exc:
            msg = f"keyring read of {service}/{account} failed: {exc}"
            raise VaultError(msg) from exc

    def put(self, service: str, account: str, secret: str) -> None:
        """Store *secret* in the keyring."""
        try:
            self._keyring.set_password(service, account, secret)
        except KeyringError as exc:
            msg = f"keyring write of {service}/{account} failed: {exc}"
            raise VaultError(msg) from exc
