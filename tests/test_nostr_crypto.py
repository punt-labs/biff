"""Tests for the one class that owns Schnorr signing and Nostr key encoding."""

from __future__ import annotations

import pytest

from biff.nostr_crypto import NostrCrypto

# BIP-340 test vector 0.
_BIP340_SECRET = bytes.fromhex("00" * 31 + "03")
_BIP340_PUBLIC = bytes.fromhex(
    "f9308a019258c31049344f85f89d5229b531c845836f99b08601f113bce036f9"
)
_BIP340_MESSAGE = bytes(32)
_BIP340_SIG = bytes.fromhex(
    "e907831f80848d1069a5371b402410364bdf1c5f8307b0084c55f1ce2dca8215"
    "25f66a4a85ea8b71e482a74f382d2ce5ebeee8fdb2172f477df4900d310536c0"
)

# NIP-19 examples.
_NIP19_PUBLIC = bytes.fromhex(
    "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
)
_NIP19_NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
_NIP19_SECRET = bytes.fromhex(
    "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
)
_NIP19_NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"


def test_public_key_matches_bip340_vector() -> None:
    assert NostrCrypto().public_key(_BIP340_SECRET) == _BIP340_PUBLIC


def test_verify_accepts_bip340_vector() -> None:
    assert NostrCrypto().verify(_BIP340_PUBLIC, _BIP340_MESSAGE, _BIP340_SIG)


def test_sign_with_vector_aux_reproduces_bip340_signature() -> None:
    sig = NostrCrypto().sign(_BIP340_SECRET, _BIP340_MESSAGE, aux=bytes(32))
    assert sig == _BIP340_SIG


def test_sign_then_verify_round_trips() -> None:
    crypto = NostrCrypto()
    secret = crypto.generate_secret()
    msg = bytes(range(32))
    assert crypto.verify(crypto.public_key(secret), msg, crypto.sign(secret, msg))


def test_verify_rejects_tampered_message() -> None:
    crypto = NostrCrypto()
    secret = crypto.generate_secret()
    sig = crypto.sign(secret, bytes(32))
    assert not crypto.verify(crypto.public_key(secret), bytes([1]) * 32, sig)


def test_verify_rejects_garbage_without_raising() -> None:
    assert not NostrCrypto().verify(bytes(32), bytes(32), bytes(64))


def test_npub_encoding_matches_nip19() -> None:
    assert NostrCrypto().npub(_NIP19_PUBLIC) == _NIP19_NPUB


def test_npub_decoding_matches_nip19() -> None:
    assert NostrCrypto().decode_npub(_NIP19_NPUB) == _NIP19_PUBLIC


def test_nsec_round_trip_matches_nip19() -> None:
    crypto = NostrCrypto()
    assert crypto.nsec(_NIP19_SECRET) == _NIP19_NSEC
    assert crypto.decode_nsec(_NIP19_NSEC) == _NIP19_SECRET


def test_decode_npub_rejects_bad_checksum() -> None:
    with pytest.raises(ValueError, match="checksum"):
        NostrCrypto().decode_npub(_NIP19_NPUB[:-1] + "q")


def test_decode_npub_rejects_nsec_prefix() -> None:
    with pytest.raises(ValueError, match="npub"):
        NostrCrypto().decode_npub(_NIP19_NSEC)
