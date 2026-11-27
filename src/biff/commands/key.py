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
from biff.nostr_bech32 import Bech32Key
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


class PublishedKeyError(ValueError):
    """The key published for an agent is unusable or is not this machine's key."""


_STORE_ERRORS = (
    OwnerKeyDeniedError,
    VaultError,
    KeyNotFoundError,
    KeyExistsError,
    OwnerKeyMismatchError,
    InvalidLifetimeError,
    PublishedKeyError,
    OSError,
)


@final
class KeyCommands:
    """The operations behind ``biff key``, returning results, never printing."""

    __slots__ = ("_clock", "_directory", "_store")

    _store: KeyStore
    _directory: NostrDirectory
    _clock: Callable[[], float]

    def __new__(
        cls, store: KeyStore, directory: NostrDirectory, clock: Callable[[], float]
    ) -> Self:
        """Create keys in *store*, publish to *directory*, tell time by *clock*."""
        self = super().__new__(cls)
        self._store = store
        self._directory = directory
        self._clock = clock
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
        npub = Bech32Key.PUBLIC.encode(pubkey)
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
            npub = Bech32Key.PUBLIC.encode(self._store.agent_pubkey(handle))
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
        npub: str | None = None,
    ) -> CommandResult:
        """Sign *agent*'s key as *owner*; the owner must be present.

        *npub* is the owner's confirmation of a key that comes from the shared
        directory, which anyone able to write it could have changed.
        """
        try:
            lifetime = self._lifetime_hours(hours, image=image)
            target = self._attestation_target(agent, npub)
            attestation = self._store.attest(
                owner,
                target,
                lifetime_seconds=lifetime * 3600,
                now=int(self._clock()),
            )
            agent_npub = Bech32Key.PUBLIC.encode(target)
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
        except (OSError, ValueError, VaultError) as exc:
            return CommandResult(text=str(exc), error=True)
        return CommandResult(text="\n".join(lines), json_data=dict(fields))

    def _owner_pubkey(self, owner: str) -> tuple[bytes, bool]:
        """Return the owner's public key and whether this call created the key."""
        try:
            return self._store.init_owner(owner), True
        except KeyExistsError:
            return self._store.owner_pubkey(owner), False

    def _attestation_target(self, agent: str, npub: str | None) -> bytes:
        """Return the key to attest: the agent's own, wherever it is kept.

        An agent image holds its key and publishes the npub; the owner signs
        from another machine that has no secret for it.  The directory is
        unauthenticated, so a key read from it is signed only when the owner
        names the same npub with ``--npub``.  A key on this machine, or one
        minted here, needs no confirmation.  A published key that is not the
        local key is a conflict the owner must settle, never a replacement.
        """
        confirmed = self._confirmed_pubkey(npub)
        local = self._store.existing_agent_pubkey(agent)
        published = self._published_pubkey(agent)
        if local is not None and published is not None and local != published:
            msg = (
                f"the npub published for {agent} differs from the key on this "
                "machine; nothing was signed. Run `biff key agent` on the host "
                "that holds the key you mean to keep, or remove one of them"
            )
            raise PublishedKeyError(msg)
        if local is not None:
            self._require_confirmation_matches(confirmed, local, agent)
            return local
        if published is None:
            if confirmed is not None:
                msg = f"no npub is published for {agent}, so there is no key to confirm"
                raise PublishedKeyError(msg)
            return self._store.agent_pubkey(agent)
        if confirmed != published:
            shown = Bech32Key.PUBLIC.encode(published)
            msg = (
                f"the npub published for {agent} is {shown}; nothing was signed. "
                "The directory is not authenticated: check that this is the key "
                f"your agent holds, then run again with `--npub {shown}`"
            )
            raise PublishedKeyError(msg)
        return published

    @staticmethod
    def _confirmed_pubkey(npub: str | None) -> bytes | None:
        if npub is None:
            return None
        try:
            return Bech32Key.PUBLIC.decode(npub)
        except ValueError as exc:
            msg = f"--npub is not a valid npub: {exc}"
            raise PublishedKeyError(msg) from exc

    @staticmethod
    def _require_confirmation_matches(
        confirmed: bytes | None, local: bytes, agent: str
    ) -> None:
        if confirmed is not None and confirmed != local:
            msg = f"--npub is not the key held here for {agent}; nothing was signed"
            raise PublishedKeyError(msg)

    def _recorded_owner(self, owner: str) -> bytes | None:
        """Return the owner key recorded here, or ``None`` for an owner never set up."""
        try:
            return self._store.owner_pubkey(owner)
        except KeyNotFoundError:
            return None

    def _published_pubkey(self, agent: str) -> bytes | None:
        npub = self._directory.npub(agent)
        if npub is None:
            return None
        try:
            return Bech32Key.PUBLIC.decode(npub)
        except ValueError as exc:
            msg = f"the npub published for {agent} is not valid: {exc}"
            raise PublishedKeyError(msg) from exc

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
            published_owner = Bech32Key.PUBLIC.decode(owner_npub)
            pinned = self._recorded_owner(owner)
            if pinned is not None and pinned != published_owner:
                return (
                    f"authorized by @{owner}: invalid; the key published for "
                    f"@{owner} is not the owner key recorded on this machine"
                )
            who = f"@{owner} ({Bech32Key.abbreviate(owner_npub)}, " + (
                "recorded on this machine)"
                if pinned is not None
                else "not recorded on this machine: published under that name only)"
            )
            attestation = OwnerAttestation(
                published_owner.hex(),
                fields["auth_conditions"],
                fields["auth_sig"],
            )
            agent = Bech32Key.PUBLIC.decode(fields["npub"]).hex()
            expiry = _iso(attestation.expires_at())
            if attestation.is_live(agent, now=int(self._clock())):
                return f"authorized by {who}: valid until {expiry}"
            if attestation.signature_is_valid(agent):
                return f"authorized by {who}: expired {expiry}"
        except (KeyError, ValueError):
            pass
        return f"authorized by @{owner}: invalid"


def _iso(timestamp: int | None) -> str:
    """Render a Unix timestamp as UTC, or ``never`` for an absent one."""
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
