"""NIP-44 v2 official vectors (paulmillr/nip44, nip44.vectors.json).

Two implementations run them: nip44_ref (coincurve + cryptography) runs all
of them; nostr-sdk's public API takes secret and public keys rather than a
conversation key, so it runs the encrypt/decrypt vectors by key pair. The
vectors come from conftest.py.
"""

from __future__ import annotations

import base64
import hashlib
import struct

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


def _x_only(secret_hex: str) -> bytes:
    return PrivateKey(bytes.fromhex(secret_hex)).public_key_xonly.format()


def test_ref_conversation_key(conversation_key_vector: dict) -> None:
    vector = conversation_key_vector
    key = nip44_ref.conversation_key(
        bytes.fromhex(vector["sec1"]), bytes.fromhex(vector["pub2"])
    )
    assert key.hex() == vector["conversation_key"]


def test_ref_message_keys(message_keys_vector: dict, nip44_vectors: dict) -> None:
    conversation = bytes.fromhex(
        nip44_vectors["valid"]["get_message_keys"]["conversation_key"]
    )
    chacha_key, chacha_nonce, hmac_key = nip44_ref.message_keys(
        conversation, bytes.fromhex(message_keys_vector["nonce"])
    )
    assert chacha_key.hex() == message_keys_vector["chacha_key"]
    assert chacha_nonce.hex() == message_keys_vector["chacha_nonce"]
    assert hmac_key.hex() == message_keys_vector["hmac_key"]


def test_ref_padded_length(padded_length_vector: list[int]) -> None:
    length, expected = padded_length_vector
    assert nip44_ref.padded_length(length) == expected


def test_ref_encrypt_decrypt(encrypt_decrypt_vector: dict) -> None:
    vector = encrypt_decrypt_vector
    key = bytes.fromhex(vector["conversation_key"])
    payload = nip44_ref.encrypt(
        vector["plaintext"], key, bytes.fromhex(vector["nonce"])
    )
    assert payload == vector["payload"]
    assert nip44_ref.decrypt(vector["payload"], key) == vector["plaintext"]


def test_ref_long_messages(long_message_vector: dict) -> None:
    vector = long_message_vector
    key = bytes.fromhex(vector["conversation_key"])
    plaintext = vector["pattern"] * vector["repeat"]
    assert hashlib.sha256(plaintext.encode()).hexdigest() == vector["plaintext_sha256"]
    payload = nip44_ref.encrypt(plaintext, key, bytes.fromhex(vector["nonce"]))
    assert hashlib.sha256(payload.encode()).hexdigest() == vector["payload_sha256"]
    assert nip44_ref.decrypt(payload, key) == plaintext


def test_ref_rejects_invalid_payloads(invalid_payload_vector: dict) -> None:
    vector = invalid_payload_vector
    with pytest.raises(nip44_ref.Nip44Error):
        nip44_ref.decrypt(vector["payload"], bytes.fromhex(vector["conversation_key"]))


def test_ref_rejects_invalid_keys(invalid_key_vector: dict) -> None:
    vector = invalid_key_vector
    with pytest.raises(ValueError):
        nip44_ref.conversation_key(
            bytes.fromhex(vector["sec1"]), bytes.fromhex(vector["pub2"])
        )


def test_ref_rejects_bad_plaintext_lengths(invalid_length: int) -> None:
    key = bytes(32)
    with pytest.raises(nip44_ref.Nip44Error):
        nip44_ref.encrypt("a" * invalid_length, key, bytes(32))


def test_nostr_sdk_decrypts_official_payloads(encrypt_decrypt_vector: dict) -> None:
    vector = encrypt_decrypt_vector
    sec1 = SecretKey.parse(vector["sec1"])
    sec2 = SecretKey.parse(vector["sec2"])
    pub1 = PublicKey.parse(_x_only(vector["sec1"]).hex())
    pub2 = PublicKey.parse(_x_only(vector["sec2"]).hex())
    assert nip44_decrypt(sec2, pub1, vector["payload"]) == vector["plaintext"]
    assert nip44_decrypt(sec1, pub2, vector["payload"]) == vector["plaintext"]


def test_nostr_sdk_and_ref_interoperate(encrypt_decrypt_vector: dict) -> None:
    vector = encrypt_decrypt_vector
    sec1 = SecretKey.parse(vector["sec1"])
    pub2 = PublicKey.parse(_x_only(vector["sec2"]).hex())
    payload = nip44_encrypt(sec1, pub2, vector["plaintext"], Nip44Version.V2)
    key = nip44_ref.conversation_key(
        bytes.fromhex(vector["sec1"]), _x_only(vector["sec2"])
    )
    assert base64.b64decode(payload)[0] == 2
    assert nip44_ref.decrypt(payload, key) == vector["plaintext"]


def test_nostr_sdk_rejects_invalid_payloads(invalid_payload_vector: dict) -> None:
    # These vectors fix a conversation key, which nostr-sdk's API cannot take,
    # so any key pair is used. That is enough for the version, base64, length
    # and MAC cases, which are all rejected before the key could matter. The
    # bad-padding vectors fail the MAC here, not the padding check; padding is
    # covered by the tests below, which use a payload with a valid MAC.
    sec = SecretKey.parse(
        "0000000000000000000000000000000000000000000000000000000000000001"
    )
    pub = PublicKey.parse(_x_only("00" * 31 + "02").hex())
    with pytest.raises(NostrSdkError):
        nip44_decrypt(sec, pub, invalid_payload_vector["payload"])


# --- Padding: payloads with a valid MAC and malformed padding -------------

SEC1 = "0000000000000000000000000000000000000000000000000000000000000001"
SEC2 = "0000000000000000000000000000000000000000000000000000000000000002"
NONCE = bytes(range(32))


def _conversation_key() -> bytes:
    return nip44_ref.conversation_key(bytes.fromhex(SEC1), _x_only(SEC2))


def _framed(declared: int, body: bytes) -> bytes:
    return struct.pack(">H", declared) + body


# Each value is the padded block as it would be before encryption. A correct
# block for a 5-byte message is 2 + 32 bytes: the prefix, the text, zeros.
MALFORMED_PADDING = {
    "zero length prefix": _framed(0, b"\0" * 32),
    "prefix longer than the data": _framed(40, b"hello" + b"\0" * 27),
    "block longer than the padded length": _framed(5, b"hello" + b"\0" * 59),
    "block shorter than the padded length": _framed(5, b"hello" + b"\0" * 10),
}


@pytest.fixture(params=sorted(MALFORMED_PADDING))
def bad_padding_payload(request: pytest.FixtureRequest) -> str:
    return nip44_ref.seal(MALFORMED_PADDING[request.param], _conversation_key(), NONCE)


def test_good_padding_control_decrypts() -> None:
    padded = _framed(5, b"hello" + b"\0" * 27)
    payload = nip44_ref.seal(padded, _conversation_key(), NONCE)
    assert nip44_ref.decrypt(payload, _conversation_key()) == "hello"
    assert (
        nip44_decrypt(
            SecretKey.parse(SEC2), PublicKey.parse(_x_only(SEC1).hex()), payload
        )
        == "hello"
    )


def test_ref_rejects_bad_padding_with_valid_mac(bad_padding_payload: str) -> None:
    with pytest.raises(nip44_ref.Nip44Error, match="invalid padding"):
        nip44_ref.decrypt(bad_padding_payload, _conversation_key())


def test_nostr_sdk_rejects_bad_padding_with_valid_mac(bad_padding_payload: str) -> None:
    with pytest.raises(NostrSdkError):
        nip44_decrypt(
            SecretKey.parse(SEC2),
            PublicKey.parse(_x_only(SEC1).hex()),
            bad_padding_payload,
        )


def test_bad_padding_is_the_only_thing_rejecting(
    bad_padding_payload: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The payloads above pass the MAC: without the padding check they decrypt.

    Replacing the padding check with a stub that skips it makes decrypt
    succeed, so the rejection in the tests above is the padding check and not
    a version, length or MAC failure.
    """

    def unpad_without_checks(padded: bytes) -> str:
        length = struct.unpack(">H", padded[:2])[0]
        return padded[2 : 2 + length].decode(errors="replace")

    monkeypatch.setattr(nip44_ref, "_unpad", unpad_without_checks)
    nip44_ref.decrypt(bad_padding_payload, _conversation_key())
