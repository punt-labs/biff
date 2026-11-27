"""Where private keys rest: the keyring, an encrypted file, the macOS keychain."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from keyring.errors import KeyringError

from biff.key_vault import KeyringVault, VaultError, VaultKey

SECRET = "ab" * 32


class _FakeKeyring:
    """Stands in for the ``keyring`` module's two functions."""

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.failure: Exception | None = None

    def get_password(self, service: str, account: str) -> str | None:
        if self.failure:
            raise self.failure
        return self.items.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        if self.failure:
            raise self.failure
        self.items[service, account] = password


def test_keyring_vault_round_trips() -> None:
    vault = KeyringVault(_FakeKeyring())
    vault.put(VaultKey("biff-agent", "claude"), SECRET)
    assert vault.get(VaultKey("biff-agent", "claude")) == SECRET


def test_keyring_vault_reports_absence_as_none() -> None:
    assert KeyringVault(_FakeKeyring()).get(VaultKey("biff-agent", "nobody")) is None


def test_keyring_vault_has_follows_put() -> None:
    vault = KeyringVault(_FakeKeyring())
    before = vault.has(VaultKey("biff-agent", "claude"))
    vault.put(VaultKey("biff-agent", "claude"), SECRET)
    assert (before, vault.has(VaultKey("biff-agent", "claude"))) == (False, True)


@pytest.mark.parametrize("operation", ["get", "has", "put"])
def test_keyring_errors_become_vault_errors_naming_the_item(operation: str) -> None:
    keyring = _FakeKeyring()
    keyring.failure = KeyringError("the Secret Service is locked")
    vault = KeyringVault(keyring)
    calls: dict[str, Callable[[], object]] = {
        "get": lambda: vault.get(VaultKey("biff-agent", "claude")),
        "has": lambda: vault.has(VaultKey("biff-agent", "claude")),
        "put": lambda: vault.put(VaultKey("biff-agent", "claude"), SECRET),
    }
    call = calls[operation]
    with pytest.raises(VaultError, match=r"biff-agent/claude.*locked"):
        call()


def test_keyring_vault_names_itself() -> None:
    assert KeyringVault(_FakeKeyring()).label == "the OS keyring"
