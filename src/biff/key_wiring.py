"""Choose where this machine keeps the owner key and the agent keys."""

from __future__ import annotations

import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, final

import keyring
import keyring.errors

from biff.key_vault import (
    KeyringModule,
    KeyringVault,
    MacKeychainVault,
    PassphraseFileVault,
    SecretVault,
)


class KeyringProbe(KeyringModule, Protocol):
    """The keyring module, plus a check that a real backend is present."""

    def is_usable(self) -> bool:
        """Return whether the keyring has a working backend."""
        ...


@final
class SystemKeyring:
    """The installed ``keyring`` package behind :class:`KeyringProbe`."""

    __slots__ = ()

    def is_usable(self) -> bool:
        """Return whether a Keychain or Secret Service backend is available.

        The fail backend that ``keyring`` falls back to on a headless Linux
        machine has priority 0 and refuses every call.
        """
        try:
            return keyring.get_keyring().priority > 0
        except (keyring.errors.KeyringError, RuntimeError):
            return False

    def get_password(self, service: str, account: str) -> str | None:
        """Return the stored password or ``None``."""
        return keyring.get_password(service, account)

    def set_password(self, service: str, account: str, password: str) -> None:
        """Store a password."""
        keyring.set_password(service, account, password)


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
    the keyring when a backend works and the encrypted file when none does.
    """
    ask = ask or getpass.getpass
    probe = backend or SystemKeyring()
    keys = home / ".punt-labs" / "biff" / "keys"
    owner: SecretVault = (
        MacKeychainVault()
        if (platform or sys.platform) == "darwin"
        else PassphraseFileVault(keys / "owner", ask)
    )
    agent: SecretVault = (
        KeyringVault(probe)
        if probe.is_usable()
        else PassphraseFileVault(keys / "agent", ask)
    )
    return owner, agent
