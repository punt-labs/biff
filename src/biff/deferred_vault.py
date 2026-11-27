"""A vault chosen on first use, so a command that needs none opens none."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Self, final

from biff.key_vault import SecretVault, VaultKey


@final
class DeferredVault:
    """A vault that is chosen on first use, so a command that needs none opens none.

    ``biff key show`` reads only public data; it must still run in a session
    that cannot reach the vault the machine chose.
    """

    __slots__ = ("_resolve", "_vault")

    _resolve: Callable[[], SecretVault]
    _vault: SecretVault | None

    def __new__(cls, resolve: Callable[[], SecretVault]) -> Self:
        """Call *resolve* for the real vault at the first get, has, put or label."""
        self = super().__new__(cls)
        self._resolve = resolve
        self._vault = None
        return self

    @property
    def label(self) -> str:
        """Name the real vault for a message to the user."""
        return self._open().label

    def get(self, key: VaultKey) -> str | None:
        """Return the secret from the real vault."""
        return self._open().get(key)

    def has(self, key: VaultKey) -> bool:
        """Return whether the real vault holds the secret."""
        return self._open().has(key)

    def put(self, key: VaultKey, secret: str) -> None:
        """Store *secret* in the real vault."""
        self._open().put(key, secret)

    def _open(self) -> SecretVault:
        if self._vault is None:
            self._vault = self._resolve()
        return self._vault


def deferred_vaults(
    resolve: Callable[[], tuple[SecretVault, SecretVault]],
) -> tuple[SecretVault, SecretVault]:
    """Return ``(owner_vault, agent_vault)`` that call *resolve* once, on first use."""
    pair = functools.cache(resolve)
    return DeferredVault(lambda: pair()[0]), DeferredVault(lambda: pair()[1])
