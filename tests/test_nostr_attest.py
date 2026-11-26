"""NIP-OA owner attestation: the Buzz test vectors and biff's receiver-side expiry."""

from __future__ import annotations

import pytest

from biff.nostr_attest import OwnerAttestation, parse_conditions
from biff.nostr_crypto import NostrCrypto
from biff.nostr_event import NostrEvent, OwnerProof, Provenance

# Test vectors from block/buzz docs/nips/NIP-OA.md at commit 298543f.
OWNER_SECRET = bytes.fromhex("00" * 31 + "01")
OWNER_PUBKEY = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
AGENT_SECRET = bytes.fromhex("00" * 31 + "02")
AGENT_PUBKEY = "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
CONDITIONS = "kind=1&created_at<1713957000"
AUTH_SIG = (
    "8b7df2575caf0a108374f8471722b233c53f9ff827a8b0f91861966c3b9dd5cb"
    "2e189eae9f49d72187674c2f5bd244145e10ff86c9f257ffe65a1ee5f108b369"
)
AUTH_TAG = ("auth", OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
EVENT_ID = "d892a65e7677e0554ebb70ee16deeb6a0727dba46450fb4bc001291d7bff971b"
EVENT_SIG = (
    "7fd38992b70b5e9e113644e51b4c8ee2227f3bdd402b1855f8786c0600394ab3"
    "ec2621742a7bad0b0000b93d4d1ae6e39525f286a3c1029f43f46c3359a6c76f"
)


def _event(tags: tuple[tuple[str, ...], ...] = (AUTH_TAG,)) -> NostrEvent:
    return NostrEvent(
        id=EVENT_ID,
        pubkey=AGENT_PUBKEY,
        created_at=1713956400,
        kind=1,
        tags=tags,
        content="owner-attested agent event",
        sig=EVENT_SIG,
    )


def test_preimage_hash_matches_vector() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert attestation.signed_digest(AGENT_PUBKEY).hex() == (
        "08cdecd55af4c28d3801fd69615dcf5cc04fab3bc134b38a840bf157197069a6"
    )


def test_vector_signature_verifies() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert attestation.signature_is_valid(AGENT_PUBKEY)


def test_vector_event_itself_is_valid() -> None:
    assert _event().is_valid()


def test_tag_round_trips() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert attestation.tag() == AUTH_TAG


def test_owner_signs_a_new_attestation_that_verifies() -> None:
    attestation = OwnerAttestation.sign(OWNER_SECRET, AGENT_PUBKEY, CONDITIONS)
    assert attestation.signature_is_valid(AGENT_PUBKEY)


def test_owner_pubkey_is_derived_from_the_signing_key() -> None:
    attestation = OwnerAttestation.sign(OWNER_SECRET, AGENT_PUBKEY, CONDITIONS)
    assert attestation.owner_pubkey == OWNER_PUBKEY


def test_signature_does_not_verify_for_another_agent() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    other = NostrCrypto().public_key(bytes.fromhex("00" * 31 + "05")).hex()
    assert not attestation.signature_is_valid(other)


def test_conditions_are_not_normalised_before_verifying() -> None:
    swapped = "created_at<1713957000&kind=1"
    attestation = OwnerAttestation(OWNER_PUBKEY, swapped, AUTH_SIG)
    assert not attestation.signature_is_valid(AGENT_PUBKEY)


NOW = 1713956500  # the receiver's clock: inside the vector's window


def _proof(event: NostrEvent, now: int = NOW) -> OwnerProof:
    return event.owner_proof(now=now)


def test_vector_event_proves_its_owner() -> None:
    assert _proof(_event()) == OwnerProof(Provenance.VALID, OWNER_PUBKEY)


def test_an_event_with_no_auth_tag_has_absent_provenance() -> None:
    event = NostrEvent.signed(AGENT_SECRET, created_at=1, kind=1, tags=(), content="")
    assert _proof(event).status is Provenance.ABSENT


@pytest.mark.parametrize(
    "tags",
    [
        pytest.param((AUTH_TAG, AUTH_TAG), id="two-auth-tags"),
        pytest.param((AUTH_TAG[:3],), id="three-elements"),
        pytest.param(((*AUTH_TAG, "extra"),), id="five-elements"),
        pytest.param(
            (("auth", OWNER_PUBKEY, "kind=1&", AUTH_SIG),), id="trailing-ampersand"
        ),
        pytest.param((("auth", OWNER_PUBKEY, "kind=01", AUTH_SIG),), id="leading-zero"),
        pytest.param(
            (("auth", OWNER_PUBKEY.upper(), CONDITIONS, AUTH_SIG),), id="upper-pubkey"
        ),
        pytest.param(
            (("auth", OWNER_PUBKEY, CONDITIONS, AUTH_SIG.upper()),), id="upper-sig"
        ),
        pytest.param(
            (("auth", f" {OWNER_PUBKEY[1:]}", CONDITIONS, AUTH_SIG),), id="space"
        ),
        pytest.param(
            (("auth", AGENT_PUBKEY.upper(), CONDITIONS, AUTH_SIG),), id="upper-self"
        ),
    ],
)
def test_malformed_auth_tags_are_malformed(
    tags: tuple[tuple[str, ...], ...],
) -> None:
    assert _proof(_event(tags)).status is Provenance.MALFORMED


def test_self_attestation_has_a_bad_owner_signature() -> None:
    tags = (("auth", AGENT_PUBKEY, CONDITIONS, AUTH_SIG),)
    event = NostrEvent.signed(
        AGENT_SECRET, created_at=NOW, kind=1, tags=tags, content="x"
    )
    assert _proof(event).status is Provenance.BAD_OWNER_SIG


def test_valid_tag_on_event_with_bad_signature_is_not_provenance() -> None:
    event = _event()
    forged = NostrEvent(
        id=event.id,
        pubkey=event.pubkey,
        created_at=event.created_at,
        kind=event.kind,
        tags=event.tags,
        content="tampered",
        sig=event.sig,
    )
    assert not forged.is_valid()
    assert _proof(forged) == OwnerProof(Provenance.BAD_EVENT_SIG, "")


def test_uppercase_event_signature_is_not_valid() -> None:
    event = _event()
    shouted = NostrEvent(
        id=event.id,
        pubkey=event.pubkey,
        created_at=event.created_at,
        kind=event.kind,
        tags=event.tags,
        content=event.content,
        sig=event.sig.upper(),
    )
    assert not shouted.is_valid()


def test_expiry_is_judged_on_the_receivers_clock_not_the_events() -> None:
    # The event claims created_at 1713956400, inside the window; the receiver's
    # clock says the window closed.
    assert _proof(_event(), now=1713957000).status is Provenance.EXPIRED


def _agent_event(conditions: str, kind: int = 1) -> NostrEvent:
    owner_sig = OwnerAttestation.sign(OWNER_SECRET, AGENT_PUBKEY, conditions)
    return NostrEvent.signed(
        AGENT_SECRET,
        created_at=NOW,
        kind=kind,
        tags=(owner_sig.tag(),),
        content="x",
    )


def test_conditions_the_event_does_not_meet_are_unmet() -> None:
    event = _agent_event("kind=7&created_at<1713957000")
    assert _proof(event).status is Provenance.CONDITIONS_UNMET


@pytest.mark.parametrize("conditions", ["", "kind=1", "created_at>5"])
def test_an_attestation_without_an_expiry_never_proves_an_owner(
    conditions: str,
) -> None:
    assert _proof(_agent_event(conditions)).status is Provenance.NO_EXPIRY


def test_a_signed_expiring_attestation_is_valid() -> None:
    event = _agent_event("created_at<1713957000")
    assert _proof(event) == OwnerProof(Provenance.VALID, OWNER_PUBKEY)


@pytest.mark.parametrize(
    "text",
    [
        "kind=1&",
        "&kind=1",
        "kind=1&&kind=2",
        "kind=01",
        "kind=65536",
        "kind =1",
        "created_at<4294967296",
        "created_at=5",
        "foo=1",
    ],
)
def test_parse_conditions_rejects_malformed(text: str) -> None:
    with pytest.raises(ValueError, match="condition"):
        parse_conditions(text)


def test_parse_conditions_accepts_empty() -> None:
    assert parse_conditions("") == ()


def test_expiry_is_read_from_the_signed_conditions() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert attestation.expires_at() == 1713957000


def test_expiry_is_checked_against_the_receivers_clock() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert attestation.is_live(AGENT_PUBKEY, now=1713956999)


def test_expired_attestation_is_not_live() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert not attestation.is_live(AGENT_PUBKEY, now=1713957000)


def test_attestation_without_expiry_is_never_live() -> None:
    attestation = OwnerAttestation.sign(OWNER_SECRET, AGENT_PUBKEY, "kind=1")
    assert not attestation.is_live(AGENT_PUBKEY, now=0)


def test_minted_attestation_expires_after_the_requested_lifetime() -> None:
    attestation = OwnerAttestation.mint(
        OWNER_SECRET, AGENT_PUBKEY, now=1_000_000, lifetime_seconds=86_400
    )
    assert attestation.expires_at() == 1_086_400


def test_is_live_is_false_not_an_exception_on_malformed_conditions() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, "kind=01", AUTH_SIG)
    assert not attestation.is_live(AGENT_PUBKEY, now=NOW)


def test_signature_check_requires_a_canonical_lowercase_agent_key() -> None:
    attestation = OwnerAttestation(OWNER_PUBKEY, CONDITIONS, AUTH_SIG)
    assert not attestation.signature_is_valid(AGENT_PUBKEY.upper())


def test_uppercase_owner_that_is_the_agent_cannot_attest_itself() -> None:
    attestation = OwnerAttestation(AGENT_PUBKEY.upper(), CONDITIONS, AUTH_SIG)
    assert not attestation.signature_is_valid(AGENT_PUBKEY)
