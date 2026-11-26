"""NIP-44 v2 official vectors (paulmillr/nip44, nip44.vectors.json).

Two implementations run them: nip44_ref (coincurve + cryptography) runs all
of them; nostr-sdk's public API takes secret and public keys rather than a
conversation key, so it runs the encrypt/decrypt vectors by key pair.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

import pytest
from coincurve import PrivateKey
from nostr_sdk import (
    Nip44Version,
    NostrSdkError,
    PublicKey,
    SecretKey,
    nip44_decrypt,
    nip44_encrypt,
)

import nip44_ref

VECTORS = json.loads((Path(__file__).parent / "nip44.vectors.json").read_text())["v2"]
VALID = VECTORS["valid"]
INVALID = VECTORS["invalid"]


def _x_only(secret_hex: str) -> bytes:
    return PrivateKey(bytes.fromhex(secret_hex)).public_key_xonly.format()


@pytest.mark.parametrize("vector", VALID["get_conversation_key"])
def test_ref_conversation_key(vector: dict) -> None:
    key = nip44_ref.conversation_key(
        bytes.fromhex(vector["sec1"]), bytes.fromhex(vector["pub2"])
    )
    assert key.hex() == vector["conversation_key"]


@pytest.mark.parametrize("vector", VALID["get_message_keys"]["keys"])
def test_ref_message_keys(vector: dict) -> None:
    conversation = bytes.fromhex(VALID["get_message_keys"]["conversation_key"])
    chacha_key, chacha_nonce, hmac_key = nip44_ref.message_keys(
        conversation, bytes.fromhex(vector["nonce"])
    )
    assert chacha_key.hex() == vector["chacha_key"]
    assert chacha_nonce.hex() == vector["chacha_nonce"]
    assert hmac_key.hex() == vector["hmac_key"]


@pytest.mark.parametrize(("length", "expected"), VALID["calc_padded_len"])
def test_ref_padded_length(length: int, expected: int) -> None:
    assert nip44_ref.padded_length(length) == expected


@pytest.mark.parametrize("vector", VALID["encrypt_decrypt"])
def test_ref_encrypt_decrypt(vector: dict) -> None:
    key = bytes.fromhex(vector["conversation_key"])
    payload = nip44_ref.encrypt(
        vector["plaintext"], key, bytes.fromhex(vector["nonce"])
    )
    assert payload == vector["payload"]
    assert nip44_ref.decrypt(vector["payload"], key) == vector["plaintext"]


@pytest.mark.parametrize("vector", VALID["encrypt_decrypt_long_msg"])
def test_ref_long_messages(vector: dict) -> None:
    key = bytes.fromhex(vector["conversation_key"])
    plaintext = vector["pattern"] * vector["repeat"]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == vector["plaintext_sha256"]
    payload = nip44_ref.encrypt(plaintext, key, bytes.fromhex(vector["nonce"]))
    assert hashlib.sha256(payload.encode()).hexdigest() == vector["payload_sha256"]
    assert nip44_ref.decrypt(payload, key) == plaintext


@pytest.mark.parametrize("vector", INVALID["decrypt"], ids=lambda v: v["note"])
def test_ref_rejects_invalid_payloads(vector: dict) -> None:
    with pytest.raises(nip44_ref.Nip44Error):
        nip44_ref.decrypt(vector["payload"], bytes.fromhex(vector["conversation_key"]))


@pytest.mark.parametrize(
    "vector", INVALID["get_conversation_key"], ids=lambda v: v["note"]
)
def test_ref_rejects_invalid_keys(vector: dict) -> None:
    with pytest.raises(ValueError):
        nip44_ref.conversation_key(
            bytes.fromhex(vector["sec1"]), bytes.fromhex(vector["pub2"])
        )


@pytest.mark.parametrize("length", INVALID["encrypt_msg_lengths"])
def test_ref_rejects_bad_plaintext_lengths(length: int) -> None:
    key = bytes(32)
    with pytest.raises(nip44_ref.Nip44Error):
        nip44_ref.encrypt("a" * length if length else "", key, bytes(32))


@pytest.mark.parametrize("vector", VALID["encrypt_decrypt"])
def test_nostr_sdk_decrypts_official_payloads(vector: dict) -> None:
    sec1 = SecretKey.parse(vector["sec1"])
    sec2 = SecretKey.parse(vector["sec2"])
    pub1 = PublicKey.parse(_x_only(vector["sec1"]).hex())
    pub2 = PublicKey.parse(_x_only(vector["sec2"]).hex())
    assert nip44_decrypt(sec2, pub1, vector["payload"]) == vector["plaintext"]
    assert nip44_decrypt(sec1, pub2, vector["payload"]) == vector["plaintext"]


@pytest.mark.parametrize("vector", VALID["encrypt_decrypt"])
def test_nostr_sdk_and_ref_interoperate(vector: dict) -> None:
    sec1 = SecretKey.parse(vector["sec1"])
    pub2 = PublicKey.parse(_x_only(vector["sec2"]).hex())
    payload = nip44_encrypt(sec1, pub2, vector["plaintext"], Nip44Version.V2)
    key = nip44_ref.conversation_key(
        bytes.fromhex(vector["sec1"]), _x_only(vector["sec2"])
    )
    assert base64.b64decode(payload)[0] == 2
    assert nip44_ref.decrypt(payload, key) == vector["plaintext"]


@pytest.mark.parametrize("vector", INVALID["decrypt"], ids=lambda v: v["note"])
def test_nostr_sdk_rejects_invalid_payloads(vector: dict) -> None:
    # The invalid-decrypt vectors fix a conversation key, which nostr-sdk's
    # API cannot take, so use any key pair: each payload is malformed before
    # the key matters (version, base64, length) or fails the MAC.
    sec = SecretKey.parse(
        "0000000000000000000000000000000000000000000000000000000000000001"
    )
    pub = PublicKey.parse(_x_only("00" * 31 + "02").hex())
    with pytest.raises(NostrSdkError):
        nip44_decrypt(sec, pub, vector["payload"])
