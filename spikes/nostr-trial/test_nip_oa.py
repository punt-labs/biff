"""Run every NIP-OA vector Buzz publishes at commit 298543f.

Sources: docs/nips/NIP-OA.md (valid vector, signed event, six invalid
vectors) and the unit tests in crates/buzz-sdk/src/nip_oa.rs (condition
grammar, strict time clauses).
"""

from __future__ import annotations

import copy
import json

import pytest
from coincurve import PrivateKey

import nip_oa
from nip_oa import InvalidAuthTagError

OWNER_SECRET = bytes.fromhex("00" * 31 + "01")
OWNER_PUBKEY = "79be667ef9dcbbac55a06295ce870b07029bfcdb2dce28d959f2815b16f81798"
AGENT_SECRET = bytes.fromhex("00" * 31 + "02")
AGENT_PUBKEY = "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5"
CONDITIONS = "kind=1&created_at<1713957000"
PREIMAGE = (
    "nostr:agent-auth:"
    "c6047f9441ed7d6d3045406e95c07cd85c778e4b8cef3ca7abac09b95c709ee5:"
    "kind=1&created_at<1713957000"
)
SHA256_PREIMAGE = "08cdecd55af4c28d3801fd69615dcf5cc04fab3bc134b38a840bf157197069a6"
AUTH_SIG = (
    "8b7df2575caf0a108374f8471722b233c53f9ff827a8b0f91861966c3b9dd5cb"
    "2e189eae9f49d72187674c2f5bd244145e10ff86c9f257ffe65a1ee5f108b369"
)
TAG = ["auth", OWNER_PUBKEY, CONDITIONS, AUTH_SIG]
TAG_BYTES_HEX = (
    "5b2261757468222c2237396265363637656639646362626163353561303632393563"
    "653837306230373032396266636462326463653238643935396632383135623136663831"
    "373938222c226b696e643d3126637265617465645f61743c3137313339353730303022"
    "2c223862376466323537356361663061313038333734663834373137323262323333633"
    "533663966663832376138623066393138363139363663336239646435636232653138396"
    "561653966343964373231383736373463326635626432343431343565313066663836633"
    "966323537666665363561316565356631303862333639225d"
)

SIGNED_EVENT = {
    "id": "d892a65e7677e0554ebb70ee16deeb6a0727dba46450fb4bc001291d7bff971b",
    "pubkey": AGENT_PUBKEY,
    "created_at": 1713956400,
    "kind": 1,
    "tags": [TAG],
    "content": "owner-attested agent event",
    "sig": (
        "7fd38992b70b5e9e113644e51b4c8ee2227f3bdd402b1855f8786c0600394ab3"
        "ec2621742a7bad0b0000b93d4d1ae6e39525f286a3c1029f43f46c3359a6c76f"
    ),
}


# --- Valid vectors -------------------------------------------------------


def test_keys_match_spec() -> None:
    assert PrivateKey(OWNER_SECRET).public_key_xonly.format().hex() == OWNER_PUBKEY
    assert PrivateKey(AGENT_SECRET).public_key_xonly.format().hex() == AGENT_PUBKEY


def test_preimage() -> None:
    assert nip_oa.preimage(AGENT_PUBKEY, CONDITIONS) == PREIMAGE.encode()


def test_sha256_of_preimage() -> None:
    assert nip_oa.digest(AGENT_PUBKEY, CONDITIONS).hex() == SHA256_PREIMAGE


def test_published_signature_verifies() -> None:
    assert nip_oa.verify_tag(TAG, AGENT_PUBKEY) == OWNER_PUBKEY


def test_tag_bytes_hex() -> None:
    encoded = json.dumps(TAG, separators=(",", ":"), ensure_ascii=False).encode()
    assert encoded.hex() == TAG_BYTES_HEX


def test_compute_tag_signs_a_verifiable_tag() -> None:
    # BIP-340 signing mixes in aux randomness, so the bytes differ from the
    # published signature; what must hold is that Buzz's vector and ours both
    # verify.
    tag = nip_oa.compute_tag(OWNER_SECRET, AGENT_PUBKEY, CONDITIONS)
    assert tag[:3] == TAG[:3]
    assert nip_oa.verify_tag(tag, AGENT_PUBKEY) == OWNER_PUBKEY


def test_signed_event_example() -> None:
    assert nip_oa.event_id(SIGNED_EVENT) == SIGNED_EVENT["id"]
    nip_oa.verify_event(SIGNED_EVENT)
    assert nip_oa.verify_event_attestation(SIGNED_EVENT) == OWNER_PUBKEY


def test_empty_conditions_round_trip() -> None:
    tag = nip_oa.compute_tag(OWNER_SECRET, AGENT_PUBKEY, "")
    assert nip_oa.verify_tag(tag, AGENT_PUBKEY) == OWNER_PUBKEY


# --- Invalid vectors from NIP-OA.md --------------------------------------


def test_reject_two_auth_tags() -> None:
    event = copy.deepcopy(SIGNED_EVENT)
    event["tags"] = [TAG, TAG]
    with pytest.raises(InvalidAuthTagError, match="more than one"):
        nip_oa.verify_event_attestation(event)


@pytest.mark.parametrize(
    "tag",
    [
        ["auth", OWNER_PUBKEY, CONDITIONS],
        ["auth", OWNER_PUBKEY, CONDITIONS, AUTH_SIG, "extra"],
    ],
    ids=["three-elements", "five-elements"],
)
def test_reject_wrong_element_count(tag: list[str]) -> None:
    with pytest.raises(InvalidAuthTagError):
        nip_oa.verify_tag(tag, AGENT_PUBKEY)


def test_reject_trailing_ampersand() -> None:
    tag = ["auth", OWNER_PUBKEY, "kind=1&", AUTH_SIG]
    with pytest.raises(InvalidAuthTagError):
        nip_oa.verify_tag(tag, AGENT_PUBKEY)


def test_reject_leading_zero() -> None:
    tag = ["auth", OWNER_PUBKEY, "kind=01", AUTH_SIG]
    with pytest.raises(InvalidAuthTagError):
        nip_oa.verify_tag(tag, AGENT_PUBKEY)


def test_reject_self_attestation() -> None:
    tag = ["auth", AGENT_PUBKEY, CONDITIONS, AUTH_SIG]
    with pytest.raises(InvalidAuthTagError, match="self-attestation"):
        nip_oa.verify_tag(tag, AGENT_PUBKEY)
    with pytest.raises(InvalidAuthTagError, match="self-attestation"):
        nip_oa.compute_tag(AGENT_SECRET, AGENT_PUBKEY, CONDITIONS)


@pytest.mark.parametrize("field", ["id", "sig"])
def test_reject_valid_tag_on_invalid_event(field: str) -> None:
    event = copy.deepcopy(SIGNED_EVENT)
    event[field] = "0" * len(event[field])
    with pytest.raises(InvalidAuthTagError):
        nip_oa.verify_event_attestation(event)


# --- Grammar and conditions, from the Rust unit tests --------------------


@pytest.mark.parametrize(
    "conditions",
    [
        "",
        "kind=1",
        "kind=0",
        "kind=65535",
        "created_at<1713957000",
        "created_at>0",
        "created_at>4294967295",
        "kind=1&created_at<1713957000",
        "kind=9&created_at>100&created_at<200",
    ],
)
def test_valid_conditions(conditions: str) -> None:
    nip_oa.parse_conditions(conditions)


@pytest.mark.parametrize(
    "conditions",
    [
        "kind=1&",
        "&kind=1",
        "kind=1&&created_at<100",
        "kind=01",
        "created_at<01713957000",
        "kind=1 ",
        " kind=1",
        "kind= 1",
        "kind=1\t",
        "foo=1",
        "Kind=1",
        "CREATED_AT<100",
        "created_at=100",
        "kind=65536",
        "created_at<4294967296",
        "kind=abc",
        "kind=-1",
        "kind=1.0",
        "kind=",
        "created_at<",
        "created_at>",
    ],
)
def test_invalid_conditions(conditions: str) -> None:
    with pytest.raises(InvalidAuthTagError):
        nip_oa.parse_conditions(conditions)


@pytest.mark.parametrize(
    "tag",
    [
        ["notauth", OWNER_PUBKEY, CONDITIONS, "a" * 128],
        ["auth", "notahex", CONDITIONS, "a" * 128],
        ["auth", OWNER_PUBKEY.upper(), CONDITIONS, "a" * 128],
        ["auth", OWNER_PUBKEY, CONDITIONS, "notasig"],
        ["auth", OWNER_PUBKEY, CONDITIONS, "A" * 128],
        ["auth", OWNER_PUBKEY, CONDITIONS, "0" * 128],
    ],
    ids=["label", "pubkey", "upper-pubkey", "sig", "upper-sig", "wrong-sig"],
)
def test_reject_malformed_or_wrong_signature(tag: list[str]) -> None:
    with pytest.raises(InvalidAuthTagError):
        nip_oa.verify_tag(tag, AGENT_PUBKEY)


def test_conditions_are_enforced_strictly() -> None:
    def event_with(conditions: str, created_at: int, kind: int = 9) -> dict:
        tag = nip_oa.compute_tag(OWNER_SECRET, AGENT_PUBKEY, conditions)
        event = {
            "pubkey": AGENT_PUBKEY,
            "created_at": created_at,
            "kind": kind,
            "tags": [tag],
            "content": "x",
        }
        event["id"] = nip_oa.event_id(event)
        event["sig"] = (
            PrivateKey(AGENT_SECRET).sign_schnorr(bytes.fromhex(event["id"])).hex()
        )
        return event

    for conditions in ("created_at<200", "created_at>200", "kind=1"):
        with pytest.raises(InvalidAuthTagError):
            nip_oa.verify_event_attestation(event_with(conditions, 200))
    with pytest.raises(InvalidAuthTagError):
        two_upper_bounds = event_with("created_at<201&created_at<200", 200)
        nip_oa.verify_event_attestation(two_upper_bounds)
    window = "kind=9&created_at>199&created_at<201"
    assert nip_oa.verify_event_attestation(event_with(window, 200)) == OWNER_PUBKEY
