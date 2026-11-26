"""BIP-340 Schnorr signing and NIP-19 key encoding behind one class.

``NostrCrypto`` is the only place biff touches secp256k1 or bech32.  Every
other module asks it to sign, verify, encode or decode, so the backend can be
swapped for a native library without touching a caller.

This backend is pure Python: no native wheel exists for every platform biff
installs on, and the arithmetic is the BIP-340 reference algorithm.  It is not
constant-time, which matters only to an attacker who can time biff's own
process from the same machine.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import final

type _Point = tuple[int, int]

_P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
_G: _Point = (
    0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
    0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8,
)
_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_CHECKSUM_LEN = 6


@final
class NostrCrypto:
    """Sign, verify and encode Nostr keys (BIP-340 and NIP-19)."""

    __slots__ = ()

    def generate_secret(self) -> bytes:
        """Return a fresh random 32-byte secret key."""
        while True:
            secret = secrets.token_bytes(32)
            if 1 <= int.from_bytes(secret, "big") < _N:
                return secret

    def public_key(self, secret: bytes) -> bytes:
        """Return the 32-byte x-only public key of *secret*."""
        point = self._base_mul(self._scalar(secret))
        return point[0].to_bytes(32, "big")

    def sign(self, secret: bytes, message: bytes, aux: bytes | None = None) -> bytes:
        """Return the 64-byte BIP-340 signature of the 32-byte *message*.

        *aux* is the 32 bytes of auxiliary randomness; fresh randomness by
        default, fixed only to reproduce a published test vector.
        """
        if len(message) != 32:
            msg = f"message must be 32 bytes, got {len(message)}"
            raise ValueError(msg)
        aux = secrets.token_bytes(32) if aux is None else aux
        d0 = self._scalar(secret)
        point = self._base_mul(d0)
        d = d0 if point[1] % 2 == 0 else _N - d0
        px = point[0].to_bytes(32, "big")
        mask = int.from_bytes(self._tagged("BIP0340/aux", aux), "big")
        t = (d ^ mask).to_bytes(32, "big")
        nonce = self._tagged("BIP0340/nonce", t + px + message)
        k0 = int.from_bytes(nonce, "big") % _N
        if k0 == 0:
            msg = "derived a zero nonce; retry with other aux data"
            raise ValueError(msg)
        r_point = self._base_mul(k0)
        k = k0 if r_point[1] % 2 == 0 else _N - k0
        rx = r_point[0].to_bytes(32, "big")
        e = self._challenge(rx, px, message)
        return rx + ((k + e * d) % _N).to_bytes(32, "big")

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

    def npub(self, public: bytes) -> str:
        """Return *public* as an ``npub1...`` string."""
        return self._bech32_encode("npub", public)

    def nsec(self, secret: bytes) -> str:
        """Return *secret* as an ``nsec1...`` string."""
        return self._bech32_encode("nsec", secret)

    def decode_npub(self, text: str) -> bytes:
        """Return the 32-byte public key named by an ``npub1...`` string."""
        return self._bech32_decode("npub", text)

    def decode_nsec(self, text: str) -> bytes:
        """Return the 32-byte secret key named by an ``nsec1...`` string."""
        return self._bech32_decode("nsec", text)

    @staticmethod
    def _scalar(secret: bytes) -> int:
        value = int.from_bytes(secret, "big")
        if len(secret) != 32 or not 1 <= value < _N:
            msg = "secret key must be 32 bytes and in [1, n-1]"
            raise ValueError(msg)
        return value

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
    def _base_mul(cls, scalar: int) -> _Point:
        point = cls._mul(_G, scalar)
        if point is None:
            msg = "scalar multiple of the generator is the point at infinity"
            raise ValueError(msg)
        return point

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

    @staticmethod
    def _polymod(values: list[int]) -> int:
        generator = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)
        chk = 1
        for value in values:
            top = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ value
            for i, g in enumerate(generator):
                chk ^= g if (top >> i) & 1 else 0
        return chk

    @staticmethod
    def _hrp_expand(hrp: str) -> list[int]:
        return [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]

    @staticmethod
    def _convert_bits(
        data: bytes | list[int], src: int, dst: int, *, pad: bool
    ) -> list[int]:
        acc = bits = 0
        out: list[int] = []
        for value in data:
            acc = (acc << src) | value
            bits += src
            while bits >= dst:
                bits -= dst
                out.append((acc >> bits) & ((1 << dst) - 1))
            acc &= (1 << bits) - 1
        if pad and bits:
            out.append((acc << (dst - bits)) & ((1 << dst) - 1))
        elif not pad and (bits >= src or acc):
            msg = "invalid padding in bech32 data"
            raise ValueError(msg)
        return out

    @classmethod
    def _bech32_encode(cls, hrp: str, data: bytes) -> str:
        words = cls._convert_bits(data, 8, 5, pad=True)
        mod = cls._polymod(cls._hrp_expand(hrp) + words + [0] * _CHECKSUM_LEN)
        mod ^= _BECH32_CONST
        check = [(mod >> 5 * (5 - i)) & 31 for i in range(_CHECKSUM_LEN)]
        return f"{hrp}1" + "".join(_CHARSET[w] for w in words + check)

    @classmethod
    def _bech32_decode(cls, expected_hrp: str, text: str) -> bytes:
        hrp, sep, payload = text.lower().rpartition("1")
        if not sep or hrp != expected_hrp:
            msg = f"expected an {expected_hrp} string, got {text[:8]!r}"
            raise ValueError(msg)
        if len(payload) <= _CHECKSUM_LEN or any(c not in _CHARSET for c in payload):
            msg = f"invalid {expected_hrp} string: bad characters or length"
            raise ValueError(msg)
        words = [_CHARSET.index(c) for c in payload]
        if cls._polymod(cls._hrp_expand(hrp) + words) != _BECH32_CONST:
            msg = f"invalid {expected_hrp} string: checksum mismatch"
            raise ValueError(msg)
        data = bytes(cls._convert_bits(words[:-_CHECKSUM_LEN], 5, 8, pad=False))
        if len(data) != 32:
            msg = f"{expected_hrp} must hold 32 bytes, got {len(data)}"
            raise ValueError(msg)
        return data
