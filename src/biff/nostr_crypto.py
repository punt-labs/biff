"""BIP-340 Schnorr signing and verification behind one class.

``NostrCrypto`` is the only place biff touches secp256k1.  Every other module
asks it to sign or verify, so the backend can be swapped without touching a
caller.

Anything that handles a secret key (deriving a public key, signing) is done
by ``nostr-sdk``, whose Rust secp256k1 is constant-time.  Verification takes
only public inputs, so it is the BIP-340 reference algorithm in pure Python:
nostr-sdk exposes no raw Schnorr verify, and coincurve has no wheel for every
platform biff installs on.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Callable
from importlib import import_module
from typing import Protocol, Self, cast, final

type _Point = tuple[int, int]

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G: _Point = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)


class _NativePublicKey(Protocol):
    def to_hex(self) -> str: ...


class _NativeKeys(Protocol):
    def public_key(self) -> _NativePublicKey: ...

    def sign_schnorr(self, message: bytes) -> str: ...


class _NativeSecretKey(Protocol):
    def from_bytes(self, data: bytes) -> object: ...


class _NativeSdk(Protocol):
    """The part of ``nostr_sdk`` biff uses; the package ships no type stubs."""

    Keys: Callable[[object], _NativeKeys]
    SecretKey: _NativeSecretKey


_SDK = cast("_NativeSdk", import_module("nostr_sdk"))


@final
class NostrCrypto:
    """Sign and verify with Nostr keys (BIP-340)."""

    __slots__ = ("_sdk",)

    _sdk: _NativeSdk

    def __new__(cls, sdk: _NativeSdk = _SDK) -> Self:
        """Sign through *sdk*, the native ``nostr_sdk`` unless a test supplies one."""
        self = super().__new__(cls)
        self._sdk = sdk
        return self

    def generate_secret(self) -> bytes:
        """Return a fresh random 32-byte secret key."""
        while True:
            secret = secrets.token_bytes(32)
            if 1 <= int.from_bytes(secret, "big") < _N:
                return secret

    def public_key(self, secret: bytes) -> bytes:
        """Return the 32-byte x-only public key of *secret*."""
        return bytes.fromhex(self._keys(secret).public_key().to_hex())

    def sign(self, secret: bytes, message: bytes) -> bytes:
        """Return the 64-byte BIP-340 signature of the 32-byte *message*."""
        if len(message) != 32:
            msg = f"message must be 32 bytes, got {len(message)}"
            raise ValueError(msg)
        return bytes.fromhex(self._keys(secret).sign_schnorr(message))

    def verify(self, public: bytes, message: bytes, signature: bytes) -> bool:
        """Return whether *signature* is a valid BIP-340 signature.

        Malformed input is an invalid signature, never an exception: a
        verifier handles hostile bytes.
        """
        if len(public) != 32 or len(message) != 32 or len(signature) != 64:
            return False
        point = self._lift_x(int.from_bytes(public, "big"))
        r = int.from_bytes(signature[:32], "big")
        s = int.from_bytes(signature[32:], "big")
        if point is None or r >= _P or s >= _N:
            return False
        e = self._challenge(signature[:32], public, message)
        r_point = self._add(self._mul(_G, s), self._mul(point, _N - e))
        return r_point is not None and r_point[1] % 2 == 0 and r_point[0] == r

    def _keys(self, secret: bytes) -> _NativeKeys:
        """Return the native key pair for *secret*, rejecting a malformed one."""
        if len(secret) != 32 or not 1 <= int.from_bytes(secret, "big") < _N:
            msg = "secret key must be 32 bytes and a valid secp256k1 scalar"
            raise ValueError(msg)
        return self._sdk.Keys(self._sdk.SecretKey.from_bytes(secret))

    @staticmethod
    def _tagged(tag: str, data: bytes) -> bytes:
        digest = hashlib.sha256(tag.encode()).digest()
        return hashlib.sha256(digest + digest + data).digest()

    @classmethod
    def _challenge(cls, rx: bytes, px: bytes, message: bytes) -> int:
        digest = cls._tagged("BIP0340/challenge", rx + px + message)
        return int.from_bytes(digest, "big") % _N

    @staticmethod
    def _lift_x(x: int) -> _Point | None:
        if x >= _P:
            return None
        y_sq = (pow(x, 3, _P) + 7) % _P
        y = pow(y_sq, (_P + 1) // 4, _P)
        if pow(y, 2, _P) != y_sq:
            return None
        return (x, y if y % 2 == 0 else _P - y)

    @staticmethod
    def _add(a: _Point | None, b: _Point | None) -> _Point | None:
        if a is None or b is None:
            return a or b
        if a[0] == b[0] and a[1] != b[1]:
            return None
        if a == b:
            lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, _P) % _P
        else:
            lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, _P) % _P
        x = (lam * lam - a[0] - b[0]) % _P
        return (x, (lam * (a[0] - x) - a[1]) % _P)

    @classmethod
    def _mul(cls, point: _Point, scalar: int) -> _Point | None:
        result: _Point | None = None
        addend: _Point | None = point
        while scalar:
            if scalar & 1:
                result = cls._add(result, addend)
            addend = cls._add(addend, addend)
            scalar >>= 1
        return result
