"""Tests for the one class that owns Schnorr signing and verification."""

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


def test_public_key_matches_bip340_vector() -> None:
    assert NostrCrypto().public_key(_BIP340_SECRET) == _BIP340_PUBLIC


def test_verify_accepts_bip340_vector() -> None:
    assert NostrCrypto().verify(_BIP340_PUBLIC, _BIP340_MESSAGE, _BIP340_SIG)


def test_signing_never_touches_the_pure_python_curve_arithmetic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Secret-key work belongs to the Rust library, never to Python integers."""

    def forbidden(*_args: object) -> None:
        msg = "pure-Python scalar multiplication used with a secret"
        raise AssertionError(msg)

    monkeypatch.setattr(NostrCrypto, "_mul", forbidden)
    monkeypatch.setattr(NostrCrypto, "_add", forbidden)
    crypto = NostrCrypto()
    secret = crypto.generate_secret()
    pubkey = crypto.public_key(secret)
    assert len(crypto.sign(secret, bytes(32))) == 64
    assert len(pubkey) == 32


def test_signature_from_the_native_library_verifies_with_our_verifier() -> None:
    crypto = NostrCrypto()
    sig = crypto.sign(_BIP340_SECRET, _BIP340_MESSAGE)
    assert crypto.verify(_BIP340_PUBLIC, _BIP340_MESSAGE, sig)


@pytest.mark.parametrize("secret", [bytes(32), b"short", bytes([255]) * 32])
def test_invalid_secret_is_a_value_error(secret: bytes) -> None:
    with pytest.raises(ValueError, match="secret key"):
        NostrCrypto().public_key(secret)


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
