"""NIP-OA owner attestation: an owner key signs an agent key's authority.

The signature covers the agent's public key and a conditions string.  Biff
always mints a ``created_at<T`` clause, and the receiver compares ``T`` with
its own clock (:meth:`OwnerAttestation.is_live`).  NIP-OA's own reading of
that clause constrains the event's self-declared ``created_at``, which the
agent controls, so it bounds nothing alone.  Reading the expiry from the
signed conditions, not from a separate field, keeps an agent from extending
its own attestation by editing the file that stores it.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Self, final

from biff.nostr_crypto import NostrCrypto

_CLAUSE_RE = re.compile(r"(kind=|created_at<|created_at>)(0|[1-9][0-9]*)")
_MAX_KIND = 65535
_MAX_TIMESTAMP = 4294967295
_DOMAIN = "nostr:agent-auth:"
_TAG_LEN = 4


@final
@dataclass(frozen=True, slots=True)
class Condition:
    """One clause of a conditions string, such as ``kind=1``."""

    operator: str
    value: int

    def satisfied_by(self, kind: int, created_at: int) -> bool:
        """Return whether an event of *kind* made at *created_at* meets this clause."""
        if self.operator == "kind=":
            return kind == self.value
        if self.operator == "created_at<":
            return created_at < self.value
        return created_at > self.value


def parse_conditions(text: str) -> tuple[Condition, ...]:
    """Return the clauses of *text*, or raise ``ValueError`` if it is malformed."""
    if not text:
        return ()
    clauses: list[Condition] = []
    for raw in text.split("&"):
        match = _CLAUSE_RE.fullmatch(raw)
        if match is None:
            msg = f"malformed condition clause {raw!r} in {text!r}"
            raise ValueError(msg)
        operator, value = match[1], int(match[2])
        limit = _MAX_KIND if operator == "kind=" else _MAX_TIMESTAMP
        if value > limit:
            msg = f"condition value out of range in clause {raw!r}"
            raise ValueError(msg)
        clauses.append(Condition(operator, value))
    return tuple(clauses)


@final
@dataclass(frozen=True, slots=True)
class OwnerAttestation:
    """An owner's signature that an agent key acts for the owner.

    All three fields are the exact strings of the ``auth`` tag: the
    conditions are never reordered or normalized, because the signature
    covers them byte for byte.
    """

    owner_pubkey: str
    conditions: str
    signature: str

    @classmethod
    def sign(cls, owner_secret: bytes, agent_pubkey: str, conditions: str) -> Self:
        """Return *owner_secret*'s attestation of *agent_pubkey* under *conditions*."""
        parse_conditions(conditions)
        crypto = NostrCrypto()
        unsigned = cls(crypto.public_key(owner_secret).hex(), conditions, "")
        sig = crypto.sign(owner_secret, unsigned.signed_digest(agent_pubkey))
        return cls(unsigned.owner_pubkey, conditions, sig.hex())

    @classmethod
    def mint(
        cls,
        owner_secret: bytes,
        agent_pubkey: str,
        *,
        now: int,
        lifetime_seconds: int,
    ) -> Self:
        """Return an attestation that expires *lifetime_seconds* after *now*."""
        conditions = f"created_at<{now + lifetime_seconds}"
        return cls.sign(owner_secret, agent_pubkey, conditions)

    @classmethod
    def from_tag(cls, tag: Sequence[str]) -> Self:
        """Return the attestation in an ``auth`` *tag*, or raise ``ValueError``."""
        if len(tag) != _TAG_LEN or tag[0] != "auth":
            msg = f"an auth tag has exactly four elements, got {len(tag)}"
            raise ValueError(msg)
        parse_conditions(tag[2])
        return cls(tag[1], tag[2], tag[3])

    def tag(self) -> tuple[str, str, str, str]:
        """Return the ``auth`` tag that carries this attestation."""
        return ("auth", self.owner_pubkey, self.conditions, self.signature)

    def signed_digest(self, agent_pubkey: str) -> bytes:
        """Return the SHA-256 digest the owner signs for *agent_pubkey*."""
        preimage = f"{_DOMAIN}{agent_pubkey}:{self.conditions}"
        return hashlib.sha256(preimage.encode()).digest()

    def signature_is_valid(self, agent_pubkey: str) -> bool:
        """Return whether the owner's signature covers *agent_pubkey* and terms."""
        if self.owner_pubkey == agent_pubkey:
            return False
        try:
            parse_conditions(self.conditions)
            owner = bytes.fromhex(self.owner_pubkey)
            sig = bytes.fromhex(self.signature)
        except ValueError:
            return False
        if self.owner_pubkey != self.owner_pubkey.lower():
            return False
        return NostrCrypto().verify(owner, self.signed_digest(agent_pubkey), sig)

    def expires_at(self) -> int | None:
        """Return the earliest ``created_at<`` bound, or ``None`` when unbounded."""
        bounds = [
            c.value
            for c in parse_conditions(self.conditions)
            if c.operator == "created_at<"
        ]
        return min(bounds, default=None)

    def is_live(self, agent_pubkey: str, *, now: int) -> bool:
        """Return whether the attestation is valid and unexpired at *now*.

        *now* is the receiver's own clock.  An attestation with no expiry is
        never live: biff mints none, so one that lacks it was made elsewhere.
        """
        expiry = self.expires_at()
        return (
            expiry is not None
            and now < expiry
            and self.signature_is_valid(agent_pubkey)
        )
