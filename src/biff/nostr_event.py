"""A NIP-01 Nostr event and the checks a client runs before trusting it."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import final

from biff.nostr_attest import OwnerAttestation, parse_conditions
from biff.nostr_crypto import NostrCrypto

_AUTH_TAG = "auth"


@final
@dataclass(frozen=True, slots=True)
class NostrEvent:
    """A signed Nostr event; ``id`` and ``sig`` are lowercase hex."""

    id: str
    pubkey: str
    created_at: int
    kind: int
    tags: tuple[tuple[str, ...], ...]
    content: str
    sig: str

    def computed_id(self) -> str:
        """Return the id NIP-01 derives from the event's other fields."""
        payload = json.dumps(
            [0, self.pubkey, self.created_at, self.kind, self.tags, self.content],
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(payload.encode()).hexdigest()

    def is_valid(self) -> bool:
        """Return whether ``id`` matches the content and ``sig`` signs ``id``."""
        if self.id != self.computed_id():
            return False
        try:
            pubkey, event_id, sig = (
                bytes.fromhex(self.pubkey),
                bytes.fromhex(self.id),
                bytes.fromhex(self.sig),
            )
        except ValueError:
            return False
        return NostrCrypto().verify(pubkey, event_id, sig)

    def owner_attestation(self) -> OwnerAttestation | None:
        """Return the event's one well-formed ``auth`` tag, else ``None``.

        An event with two ``auth`` tags has none, and so does one whose tag
        is malformed.
        """
        auth_tags = [t for t in self.tags if t and t[0] == _AUTH_TAG]
        if len(auth_tags) != 1:
            return None
        try:
            return OwnerAttestation.from_tag(auth_tags[0])
        except ValueError:
            return None

    def verified_owner(self) -> str | None:
        """Return the owner's hex pubkey when the event proves owner provenance.

        The event must be valid, carry one ``auth`` tag whose signature covers
        this event's author, and satisfy every condition.  The result never
        names the author; it is provenance, not authorship.
        """
        attestation = self.owner_attestation()
        if attestation is None or not self.is_valid():
            return None
        if not attestation.signature_is_valid(self.pubkey):
            return None
        clauses = parse_conditions(attestation.conditions)
        if all(c.satisfied_by(self.kind, self.created_at) for c in clauses):
            return attestation.owner_pubkey
        return None
