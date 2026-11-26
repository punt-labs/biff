"""``biff key`` — create Nostr keys, publish their public halves, attest agents."""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Self, final

from biff.commands._result import CommandResult
from biff.key_store import KeyStore, OwnerKeyDeniedError
from biff.key_vault import VaultError
from biff.nostr_attest import OwnerAttestation
from biff.nostr_crypto import NostrCrypto
from biff.nostr_directory import NostrDirectory

LIFETIME_ENV = "BIFF_ATTESTATION_HOURS"
DEVELOPER_HOURS = 24
IMAGE_HOURS = 7 * 24
_STORE_ERRORS = (OwnerKeyDeniedError, VaultError, ValueError, KeyError)


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
        """Create *owner*'s key, store its private half, publish its npub."""
        try:
            npub = self._crypto.npub(self._store.init_owner(owner))
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        self._directory.publish(owner, {"npub": npub})
        return CommandResult(text=f"your npub: {npub}", json_data={"npub": npub})

    def agent(self, handle: str) -> CommandResult:
        """Create *handle*'s agent key if it has none and publish its npub."""
        try:
            npub = self._crypto.npub(self._store.agent_pubkey(handle))
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        self._directory.publish(handle, {"npub": npub})
        return CommandResult(
            text=f"{handle} npub: {npub}", json_data={"handle": handle, "npub": npub}
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
            now = int(self._clock())
            attestation = self._store.attest(
                owner, agent, lifetime_seconds=lifetime * 3600, now=now
            )
        except _STORE_ERRORS as exc:
            return CommandResult(text=str(exc), error=True)
        agent_npub = self._crypto.npub(self._store.agent_pubkey(agent))
        owner_npub = self._crypto.npub(bytes.fromhex(attestation.owner_pubkey))
        expires = attestation.expires_at()
        self._directory.publish(
            agent,
            {
                "npub": agent_npub,
                "owner_npub": owner_npub,
                "auth_conditions": attestation.conditions,
                "auth_sig": attestation.signature,
                "auth_expires_at": str(expires),
            },
        )
        text = f"{agent} attested by {owner} for {lifetime}h (until {_iso(expires)})"
        return CommandResult(
            text=text, json_data={"agent": agent, "expires_at": expires}
        )

    def show(self, handle: str) -> CommandResult:
        """Report *handle*'s published key and attestation state."""
        fields = self._directory.fields(handle)
        if "npub" not in fields:
            return CommandResult(text=f"no key published for {handle}", error=True)
        lines = [f"{handle}: {fields['npub']}"]
        if "auth_sig" in fields:
            lines.append(
                f"  authorized by {fields.get('owner_npub', '?')}: "
                f"{self._attestation_state(fields)}"
            )
        return CommandResult(text="\n".join(lines), json_data=dict(fields))

    def _lifetime_hours(self, hours: int | None, *, image: bool) -> int:
        """Return the lifetime: the option, else the environment, else the default."""
        if hours is None and (configured := os.environ.get(LIFETIME_ENV)):
            try:
                hours = int(configured)
            except ValueError:
                msg = f"{LIFETIME_ENV} must be whole hours, got {configured!r}"
                raise ValueError(msg) from None
        if hours is None:
            return IMAGE_HOURS if image else DEVELOPER_HOURS
        if hours < 1:
            msg = f"attestation lifetime must be at least 1 hour, got {hours}"
            raise ValueError(msg)
        return hours

    def _attestation_state(self, fields: dict[str, str]) -> str:
        """Describe the stored attestation, judged on this machine's clock."""
        try:
            agent = self._crypto.decode_npub(fields["npub"]).hex()
            owner = self._crypto.decode_npub(fields["owner_npub"]).hex()
            attestation = OwnerAttestation(
                owner, fields["auth_conditions"], fields["auth_sig"]
            )
            if attestation.is_live(agent, now=int(self._clock())):
                return f"valid until {_iso(attestation.expires_at())}"
            if attestation.signature_is_valid(agent):
                return f"expired {_iso(attestation.expires_at())}"
        except (KeyError, ValueError):
            pass
        return "invalid"


def _iso(timestamp: int | None) -> str:
    """Render a Unix timestamp as UTC, or ``never`` for an absent one."""
    if timestamp is None:
        return "never"
    return datetime.fromtimestamp(timestamp, UTC).strftime("%Y-%m-%d %H:%M UTC")
