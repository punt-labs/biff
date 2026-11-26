"""``biff key`` — create Nostr keys, publish their public halves, attest agents."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Self, final

from biff.commands._result import CommandResult
from biff.key_store import (
    KeyExistsError,
    KeyNotFoundError,
    KeyStore,
    OwnerKeyDeniedError,
    OwnerKeyMismatchError,
)
from biff.key_vault import VaultError
from biff.nostr_attest import OwnerAttestation
from biff.nostr_crypto import NostrCrypto
from biff.nostr_directory import NostrDirectory

LIFETIME_ENV = "BIFF_ATTESTATION_HOURS"
DEVELOPER_HOURS = 24
IMAGE_HOURS = 7 * 24
_ALLOW_WARNING = (
    "When macOS asks to let `security` read this key, click Allow, never click "
    "Always Allow: that lets any program of yours read the key without asking."
)


class InvalidLifetimeError(ValueError):
    """An attestation lifetime that is not a positive number of hours."""


_STORE_ERRORS = (
    OwnerKeyDeniedError,
    VaultError,
    KeyNotFoundError,
    KeyExistsError,
    OwnerKeyMismatchError,
    InvalidLifetimeError,
)


@final
class KeyCommands:
    """The operations behind ``biff key``, returning results, never printing."""

    __slots__ = ("_clock", "_crypto", "_directory", "_store")

    _store: KeyStore
    _directory: NostrDirectory
    _clock: Callable[[], float]
    _crypto: NostrCrypto

    def __new__(
        cls, store: KeyStore, directory: NostrDirectory, clock: Callable[[], float]
    ) -> Self:
        """Create keys in *store*, publish to *directory*, tell time by *clock*."""
        self = super().__new__(cls)
        self._store = store
        self._directory = directory
        self._clock = clock
        self._crypto = NostrCrypto()
        return self

    def init(self, owner: str) -> CommandResult:
        """Create *owner*'s key, store its private half, publish its npub.

        Run again for an owner whose key exists, it republishes the recorded
        npub: the key survives a publish that failed.
        """
        try:
            pubkey, created = self._owner_pubkey(owner)
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        npub = self._crypto.npub(pubkey)
        try:
            self._directory.publish(owner, {"npub": npub})
        except OSError as exc:
            text = (
                f"owner key stored; publishing npub failed: {exc}; "
                "run `biff key init` again to retry"
            )
            return CommandResult(text=text, error=True)
        prefix = "your npub" if created else "already initialised; republished npub"
        text = f"{prefix}: {npub}\n{_ALLOW_WARNING}"
        return CommandResult(text=text, json_data={"npub": npub, "created": created})

    def agent(self, handle: str) -> CommandResult:
        """Create *handle*'s agent key if it has none and publish its npub."""
        try:
            npub = self._crypto.npub(self._store.agent_pubkey(handle))
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        vault = self._store.agent_vault_label
        try:
            self._directory.publish(handle, {"npub": npub})
        except OSError as exc:
            text = f"agent key stored in {vault}; publishing npub failed: {exc}"
            return CommandResult(text=text, error=True)
        return CommandResult(
            text=f"{handle} npub: {npub} (key in {vault})",
            json_data={"handle": handle, "npub": npub, "vault": vault},
        )

    def attest(
        self,
        owner: str,
        agent: str,
        *,
        hours: int | None = None,
        image: bool = False,
    ) -> CommandResult:
        """Sign *agent*'s key as *owner*; the owner must be present."""
        try:
            lifetime = self._lifetime_hours(hours, image=image)
            attestation = self._store.attest(
                owner,
                agent,
                lifetime_seconds=lifetime * 3600,
                now=int(self._clock()),
            )
            agent_npub = self._crypto.npub(self._store.agent_pubkey(agent))
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        try:
            self._directory.publish(
                agent,
                {
                    "npub": agent_npub,
                    "owner": owner,
                    "auth_conditions": attestation.conditions,
                    "auth_sig": attestation.signature,
                },
            )
        except OSError as exc:
            return CommandResult(text=f"attestation signed; {exc}", error=True)
        expires = attestation.expires_at()
        text = f"{agent} attested by {owner} for {lifetime}h (until {_iso(expires)})"
        return CommandResult(
            text=text, json_data={"agent": agent, "expires_at": expires}
        )

    def show(self, handle: str) -> CommandResult:
        """Report *handle*'s published key and attestation state."""
        try:
            fields = self._directory.fields(handle)
            if "npub" not in fields:
                return CommandResult(text=f"no key published for {handle}", error=True)
            lines = [f"{handle}: {fields['npub']}"]
            if "auth_sig" in fields:
                lines.append(f"  {self._attestation_line(fields)}")
        except OSError as exc:
            return CommandResult(text=str(exc), error=True)
        return CommandResult(text="\n".join(lines), json_data=dict(fields))

    def _owner_pubkey(self, owner: str) -> tuple[bytes, bool]:
        """Return the owner's public key and whether this call created the key."""
        try:
            return self._store.init_owner(owner), True
        except KeyExistsError:
            return self._store.owner_pubkey(owner), False

    @staticmethod
    def _lifetime_hours(hours: int | None, *, image: bool) -> int:
        """Return the lifetime: the option, else the environment, else the default."""
        if hours is None and (configured := os.environ.get(LIFETIME_ENV)):
            try:
                hours = int(configured)
            except ValueError:
                msg = f"{LIFETIME_ENV} must be whole hours, got {configured!r}"
                raise InvalidLifetimeError(msg) from None
        if hours is None:
            return IMAGE_HOURS if image else DEVELOPER_HOURS
        if hours < 1:
            msg = f"attestation lifetime must be at least 1 hour, got {hours}"
            raise InvalidLifetimeError(msg)
        return hours

    def _attestation_line(self, fields: dict[str, str]) -> str:
        """Describe the stored attestation, judged on this machine's clock.

        The owner is looked up by the handle the agent's namespace names and
        checked against that owner's own published key, so an agent that
        rewrites its namespace cannot name a key it controls.
        """
        owner = fields.get("owner", "")
        owner_npub = self._directory.npub(owner) if owner else None
        if owner_npub is None:
            return f"authorized by @{owner or '?'}: no published key for the owner"
        try:
            attestation = OwnerAttestation(
                self._crypto.decode_npub(owner_npub).hex(),
                fields["auth_conditions"],
                fields["auth_sig"],
            )
            agent = self._crypto.decode_npub(fields["npub"]).hex()
            expiry = _iso(attestation.expires_at())
            if attestation.is_live(agent, now=int(self._clock())):
                return f"authorized by @{owner}: valid until {expiry}"
            if attestation.signature_is_valid(agent):
                return f"authorized by @{owner}: expired {expiry}"
        except (KeyError, ValueError):
            pass
        return f"authorized by @{owner}: invalid"


def _iso(timestamp: int | None) -> str:
    """Render a Unix timestamp as UTC, or ``never`` for an absent one."""
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
