"""NIP-44 v2 in about sixty lines: coincurve (ECDH) + cryptography (ChaCha20).

A reference to cross-check library output, and the measure of what a
dependency-free-of-Nostr-libraries implementation costs.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import struct

from coincurve import PrivateKey, PublicKey
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

SALT = b"nip44-v2"
MAX_PLAINTEXT = 65535


class Nip44Error(ValueError):
    """Payload or key rejected."""


def conversation_key(secret: bytes, x_only_pub: bytes) -> bytes:
    shared = PublicKey(b"\x02" + x_only_pub).multiply(PrivateKey(secret).secret)
    shared_x = shared.format(compressed=True)[1:]
    return hmac.new(SALT, shared_x, hashlib.sha256).digest()


def message_keys(key: bytes, nonce: bytes) -> tuple[bytes, bytes, bytes]:
    if len(key) != 32 or len(nonce) != 32:
        raise Nip44Error("conversation key and nonce must be 32 bytes")
    okm = b""
    block = b""
    for counter in range(1, 4):
        block = hmac.new(key, block + nonce + bytes([counter]), hashlib.sha256).digest()
        okm += block
    return okm[:32], okm[32:44], okm[44:76]


def padded_length(unpadded: int) -> int:
    if unpadded <= 32:
        return 32
    next_power = 1 << (unpadded - 1).bit_length()
    chunk = 32 if next_power <= 256 else next_power // 8
    return chunk * ((unpadded - 1) // chunk + 1)


def _pad(plaintext: str) -> bytes:
    data = plaintext.encode()
    if not 1 <= len(data) <= MAX_PLAINTEXT:
        raise Nip44Error("plaintext length out of range")
    return (
        struct.pack(">H", len(data))
        + data
        + b"\0" * (padded_length(len(data)) - len(data))
    )


def _unpad(padded: bytes) -> str:
    length = struct.unpack(">H", padded[:2])[0]
    data = padded[2 : 2 + length]
    if length == 0 or len(data) != length or len(padded) != 2 + padded_length(length):
        raise Nip44Error("invalid padding")
    return data.decode()


def _chacha(key: bytes, nonce12: bytes, data: bytes) -> bytes:
    cipher = Cipher(algorithms.ChaCha20(key, b"\0\0\0\0" + nonce12), mode=None)
    return cipher.encryptor().update(data)


def seal(padded: bytes, key: bytes, nonce: bytes) -> str:
    """Encrypt and MAC bytes that are already padded.

    `encrypt` pads correctly and calls this. Tests call it directly with
    malformed padding to build a payload whose MAC is valid.
    """
    chacha_key, chacha_nonce, hmac_key = message_keys(key, nonce)
    ciphertext = _chacha(chacha_key, chacha_nonce, padded)
    mac = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    return base64.b64encode(b"\x02" + nonce + ciphertext + mac).decode()


def encrypt(plaintext: str, key: bytes, nonce: bytes) -> str:
    return seal(_pad(plaintext), key, nonce)


def decrypt(payload: str, key: bytes) -> str:
    if not payload or payload[0] == "#":
        raise Nip44Error("unsupported encoding or version")
    if not 132 <= len(payload) <= 87472:
        raise Nip44Error("invalid payload length")
    try:
        data = base64.b64decode(payload, validate=True)
    except ValueError as error:
        raise Nip44Error("invalid base64") from error
    if not 99 <= len(data) <= 65603:
        raise Nip44Error("invalid data length")
    if data[0] != 2:
        raise Nip44Error(f"unknown version {data[0]}")
    nonce, ciphertext, mac = data[1:33], data[33:-32], data[-32:]
    chacha_key, chacha_nonce, hmac_key = message_keys(key, nonce)
    expected = hmac.new(hmac_key, nonce + ciphertext, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, mac):
        raise Nip44Error("invalid MAC")
    return _unpad(_chacha(chacha_key, chacha_nonce, ciphertext))
