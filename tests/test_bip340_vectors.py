"""The official BIP-340 test vectors (bitcoin/bips, bip-0340/test-vectors.csv).

``tests/bip340_test_vectors.csv`` is vendored unchanged.  Indices 15 to 18 sign
messages that are not 32 bytes; Nostr always signs a 32-byte digest, so
``NostrCrypto`` rejects those lengths and the test skips them.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pytest

from biff.nostr_crypto import NostrCrypto

_CSV = Path(__file__).with_name("bip340_test_vectors.csv")
_DIGEST_LEN = 32


@dataclass(frozen=True)
class Vector:
    index: int
    secret: bytes
    public: bytes
    message: bytes
    signature: bytes
    ok: bool
    comment: str


def _load() -> list[Vector]:
    with _CSV.open(newline="", encoding="utf-8") as handle:
        return [
            Vector(
                int(row["index"]),
                bytes.fromhex(row["secret key"]),
                bytes.fromhex(row["public key"]),
                bytes.fromhex(row["message"]),
                bytes.fromhex(row["signature"]),
                row["verification result"] == "TRUE",
                row["comment"],
            )
            for row in csv.DictReader(handle)
        ]


_VECTORS = [v for v in _load() if len(v.message) == _DIGEST_LEN]
_IDS = [f"vector-{v.index}" for v in _VECTORS]


def test_vectors_zero_to_fourteen_are_all_present() -> None:
    assert {v.index for v in _VECTORS} >= set(range(15))


@pytest.mark.parametrize("vector", _VECTORS, ids=_IDS)
def test_verify_matches_the_published_result(vector: Vector) -> None:
    result = NostrCrypto().verify(vector.public, vector.message, vector.signature)
    assert result is vector.ok, vector.comment


@pytest.mark.parametrize(
    "vector", [v for v in _VECTORS if v.secret], ids=lambda v: f"vector-{v.index}"
)
def test_the_published_secret_derives_the_published_public_key(
    vector: Vector,
) -> None:
    assert NostrCrypto().public_key(vector.secret) == vector.public


@pytest.mark.parametrize(
    "vector", [v for v in _VECTORS if v.secret], ids=lambda v: f"vector-{v.index}"
)
def test_a_native_signature_verifies_under_the_published_key(vector: Vector) -> None:
    crypto = NostrCrypto()
    sig = crypto.sign(vector.secret, vector.message)
    assert crypto.verify(vector.public, vector.message, sig)
