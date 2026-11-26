"""Minimal NIP-OA (owner attestation) and NIP-01 event checks.

Spike code that proves the NIP-OA vectors in Python. Mirrors docs/nips/NIP-OA.md and
crates/buzz-sdk/src/nip_oa.rs at Buzz commit 298543f.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

from coincurve import PrivateKey, PublicKeyXOnly

DOMAIN = "nostr:agent-auth:"
_HEX64 = re.compile(r"[0-9a-f]{64}")
_HEX128 = re.compile(r"[0-9a-f]{128}")
_DECIMAL = re.compile(r"0|[1-9][0-9]*")


class InvalidAuthTagError(ValueError):
    """The auth tag, or the event it is attached to, must be rejected."""


@dataclass(frozen=True)
class AuthTag:
    owner: str
    conditions: str
    sig: str


def _canonical_decimal(text: str, maximum: int) -> int:
    if not _DECIMAL.fullmatch(text):
        raise InvalidAuthTagError(f"not a canonical decimal: {text!r}")
    value = int(text)
    if value > maximum:
        raise InvalidAuthTagError(f"{value} exceeds {maximum}")
    return value


def parse_conditions(conditions: str) -> list[tuple[str, int]]:
    """Return (operator, value) clauses; raise on any malformed input."""
    if conditions == "":
        return []
    if not conditions.isascii() or any(c.isspace() for c in conditions):
        raise InvalidAuthTagError("conditions must be ASCII without whitespace")
    clauses: list[tuple[str, int]] = []
    for clause in conditions.split("&"):
        if clause.startswith("kind="):
            clauses.append(("kind=", _canonical_decimal(clause[5:], 65535)))
        elif clause.startswith("created_at<"):
            clauses.append(("created_at<", _canonical_decimal(clause[11:], 2**32 - 1)))
        elif clause.startswith("created_at>"):
            clauses.append(("created_at>", _canonical_decimal(clause[11:], 2**32 - 1)))
        else:
            raise InvalidAuthTagError(f"unsupported or empty clause: {clause!r}")
    return clauses


def parse_tag(tag: object) -> AuthTag:
    """Structural checks only (no crypto)."""
    if not isinstance(tag, list) or len(tag) != 4:
        raise InvalidAuthTagError("auth tag must be a four-element list")
    label, owner, conditions, sig = tag
    if label != "auth":
        raise InvalidAuthTagError("first element must be 'auth'")
    if not (isinstance(owner, str) and _HEX64.fullmatch(owner)):
        raise InvalidAuthTagError("owner must be 64 lowercase hex characters")
    if not isinstance(conditions, str):
        raise InvalidAuthTagError("conditions must be a string")
    parse_conditions(conditions)
    if not (isinstance(sig, str) and _HEX128.fullmatch(sig)):
        raise InvalidAuthTagError("signature must be 128 lowercase hex characters")
    return AuthTag(owner, conditions, sig)


def preimage(agent_pubkey: str, conditions: str) -> bytes:
    return f"{DOMAIN}{agent_pubkey}:{conditions}".encode()


def digest(agent_pubkey: str, conditions: str) -> bytes:
    return hashlib.sha256(preimage(agent_pubkey, conditions)).digest()


def compute_tag(owner_secret: bytes, agent_pubkey: str, conditions: str) -> list[str]:
    owner = PrivateKey(owner_secret).public_key_xonly.format().hex()
    if owner == agent_pubkey:
        raise InvalidAuthTagError("self-attestation")
    parse_conditions(conditions)
    sig = PrivateKey(owner_secret).sign_schnorr(digest(agent_pubkey, conditions))
    return ["auth", owner, conditions, sig.hex()]


def event_id(event: dict) -> str:
    """NIP-01 id: sha256 of the compact JSON serialization."""
    payload = [
        0,
        event["pubkey"],
        event["created_at"],
        event["kind"],
        event["tags"],
        event["content"],
    ]
    text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def verify_event(event: dict) -> None:
    """NIP-01 core rules: id matches content, sig verifies for pubkey."""
    if event_id(event) != event["id"]:
        raise InvalidAuthTagError("event id does not match content")
    key = PublicKeyXOnly(bytes.fromhex(event["pubkey"]))
    if not key.verify(bytes.fromhex(event["sig"]), bytes.fromhex(event["id"])):
        raise InvalidAuthTagError("event signature invalid")


def verify_tag(tag: object, agent_pubkey: str) -> str:
    """Verify signature and structure; return the owner pubkey hex."""
    parsed = parse_tag(tag)
    if parsed.owner == agent_pubkey:
        raise InvalidAuthTagError("self-attestation")
    key = PublicKeyXOnly(bytes.fromhex(parsed.owner))
    message = digest(agent_pubkey, parsed.conditions)
    if not key.verify(bytes.fromhex(parsed.sig), message):
        raise InvalidAuthTagError("owner signature invalid")
    return parsed.owner


def verify_event_attestation(event: dict) -> str | None:
    """Return the attesting owner, or None when the event has no auth tag.

    Raises InvalidAuthTagError when the event is invalid, carries two auth tags,
    or the tag fails verification or its conditions.
    """
    auth_tags = [t for t in event["tags"] if t and t[0] == "auth"]
    if not auth_tags:
        return None
    if len(auth_tags) > 1:
        raise InvalidAuthTagError("more than one auth tag")
    verify_event(event)
    owner = verify_tag(auth_tags[0], event["pubkey"])
    for operator, value in parse_conditions(auth_tags[0][2]):
        if operator == "kind=" and event["kind"] != value:
            raise InvalidAuthTagError("kind clause not satisfied")
        if operator == "created_at<" and not event["created_at"] < value:
            raise InvalidAuthTagError("created_at< clause not satisfied")
        if operator == "created_at>" and not event["created_at"] > value:
            raise InvalidAuthTagError("created_at> clause not satisfied")
    return owner
