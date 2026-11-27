"""An in-memory :class:`SecretVault` for tests of key custody."""

from __future__ import annotations

from biff.key_vault import VaultError, VaultKey


class MemoryVault:
    """Secrets in a dict, with the reads and failures a test needs to observe."""

    label = "memory"

    def __init__(self) -> None:
        self.items: dict[tuple[str, str], str] = {}
        self.reads: list[VaultKey] = []
        self.fail_get = False
        self.fail_put_after: int | None = None

    def get(self, key: VaultKey) -> str | None:
        self.reads.append(key)
        if self.fail_get:
            msg = "the keychain is locked"
            raise VaultError(msg)
        return self.items.get((key.service, key.account))

    def has(self, key: VaultKey) -> bool:
        return (key.service, key.account) in self.items

    def put(self, key: VaultKey, secret: str) -> None:
        if self.fail_put_after is not None:
            if self.fail_put_after <= 0:
                msg = "the passphrases do not match"
                raise VaultError(msg)
            self.fail_put_after -= 1
        self.items[key.service, key.account] = secret
