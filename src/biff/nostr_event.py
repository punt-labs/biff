"""A NIP-01 Nostr event and the checks a client runs before trusting it."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from typing import Self, final

from biff.nostr_attest import OwnerAttestation, parse_conditions
from biff.nostr_crypto import NostrCrypto

_AUTH_TAG = "auth"
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_HEX128_RE = re.compile(r"[0-9a-f]{128}")


class Provenance(Enum):
    """What an event proves about the owner behind its author."""

    ABSENT = "absent"
    MALFORMED = "malformed"
    BAD_EVENT_SIG = "bad_event_sig"
    BAD_OWNER_SIG = "bad_owner_sig"
    CONDITIONS_UNMET = "conditions_unmet"
    NO_EXPIRY = "no_expiry"
    EXPIRED = "expired"
    VALID = "valid"


@final
@dataclass(frozen=True, slots=True)
class OwnerProof:
    """The verdict on an event's owner attestation.

    ``owner`` is the owner's hex pubkey when ``status`` is ``VALID`` and empty
    otherwise: provenance is never read from an event that failed a check.
    """

    status: Provenance
    owner: str = ""


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

    @classmethod
    def signed(
        cls,
        secret: bytes,
        *,
        created_at: int,
        kind: int,
        tags: tuple[tuple[str, ...], ...],
        content: str,
    ) -> Self:
        """Return an event authored and signed by the holder of *secret*."""
        crypto = NostrCrypto()
        unsigned = cls(
            "", crypto.public_key(secret).hex(), created_at, kind, tags, content, ""
        )
        event_id = unsigned.computed_id()
        sig = crypto.sign(secret, bytes.fromhex(event_id)).hex()
        return cls(event_id, unsigned.pubkey, created_at, kind, tags, content, sig)

    def is_valid(self) -> bool:
        """Return whether ``id`` matches the content and ``sig`` signs ``id``.

        ``id`` and ``pubkey`` must be 64 lowercase hex digits and ``sig`` 128.
        """
        if (
            not _HEX64_RE.fullmatch(self.id)
            or not _HEX64_RE.fullmatch(self.pubkey)
            or not _HEX128_RE.fullmatch(self.sig)
            or self.id != self.computed_id()
        ):
            return False
        return NostrCrypto().verify(
            bytes.fromhex(self.pubkey), bytes.fromhex(self.id), bytes.fromhex(self.sig)
        )

    def owner_proof(self, *, now: int) -> OwnerProof:
        """Judge the event's owner attestation on the receiver's clock *now*.

        The event must be validly signed by its author, carry exactly one
        well-formed ``auth`` tag whose owner signature covers that author,
        meet every condition, and name an expiry that *now* has not reached.
        *now* is the verifier's own clock: the event's ``created_at`` is the
        author's claim and cannot keep an attestation alive.  The proof never
        names the author; it is provenance, not authorship.
        """
        auth_tags = [t for t in self.tags if t and t[0] == _AUTH_TAG]
        if not auth_tags:
            return OwnerProof(Provenance.ABSENT)
        if len(auth_tags) > 1:
            return OwnerProof(Provenance.MALFORMED)
        try:
            attestation = OwnerAttestation.from_tag(auth_tags[0])
            clauses = parse_conditions(attestation.conditions)
            expiry = attestation.expires_at()
        except ValueError:
            return OwnerProof(Provenance.MALFORMED)
        if not self.is_valid():
            return OwnerProof(Provenance.BAD_EVENT_SIG)
        if not attestation.signature_is_valid(self.pubkey):
            return OwnerProof(Provenance.BAD_OWNER_SIG)
        if not all(c.satisfied_by(self.kind, self.created_at) for c in clauses):
            return OwnerProof(Provenance.CONDITIONS_UNMET)
        if expiry is None:
            return OwnerProof(Provenance.NO_EXPIRY)
        if now >= expiry:
            return OwnerProof(Provenance.EXPIRED)
        return OwnerProof(Provenance.VALID, attestation.owner_pubkey)
