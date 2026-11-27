"""NIP-19 bech32 encoding of 32-byte Nostr keys: ``npub1...`` and ``nsec1...``."""

from __future__ import annotations

from typing import ClassVar, Self, final

_CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"
_BECH32_CONST = 1
_CHECKSUM_LEN = 6
_KEY_BYTES = 32
_GENERATOR = (0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3)


@final
class Bech32Key:
    """The bech32 text form of a 32-byte key, under one human-readable prefix."""

    __slots__ = ("_hrp",)

    PUBLIC: ClassVar[Bech32Key]
    SECRET: ClassVar[Bech32Key]

    _hrp: str

    def __new__(cls, hrp: str) -> Self:
        """Encode and decode strings that start ``<hrp>1``."""
        self = super().__new__(cls)
        self._hrp = hrp
        return self

    def encode(self, data: bytes) -> str:
        """Return *data* as ``<hrp>1...`` text."""
        words = self._convert_bits(data, 8, 5, pad=True)
        mod = self._polymod(self._hrp_expand() + words + [0] * _CHECKSUM_LEN)
        mod ^= _BECH32_CONST
        check = [(mod >> 5 * (5 - i)) & 31 for i in range(_CHECKSUM_LEN)]
        return f"{self._hrp}1" + "".join(_CHARSET[w] for w in words + check)

    def decode(self, text: str) -> bytes:
        """Return the 32 bytes named by *text*, or raise ``ValueError``."""
        hrp, sep, payload = text.lower().rpartition("1")
        if not sep or hrp != self._hrp:
            msg = f"expected an {self._hrp} string, got {text[:8]!r}"
            raise ValueError(msg)
        if len(payload) <= _CHECKSUM_LEN or any(c not in _CHARSET for c in payload):
            msg = f"invalid {self._hrp} string: bad characters or length"
            raise ValueError(msg)
        words = [_CHARSET.index(c) for c in payload]
        if self._polymod(self._hrp_expand() + words) != _BECH32_CONST:
            msg = f"invalid {self._hrp} string: checksum mismatch"
            raise ValueError(msg)
        data = bytes(self._convert_bits(words[:-_CHECKSUM_LEN], 5, 8, pad=False))
        if len(data) != _KEY_BYTES:
            msg = f"{self._hrp} must hold {_KEY_BYTES} bytes, got {len(data)}"
            raise ValueError(msg)
        return data

    @staticmethod
    def abbreviate(text: str) -> str:
        """Clip *text* to its first ten and last five characters."""
        return f"{text[:10]}…{text[-5:]}"

    def _hrp_expand(self) -> list[int]:
        return [ord(c) >> 5 for c in self._hrp] + [0] + [ord(c) & 31 for c in self._hrp]

    @staticmethod
    def _polymod(values: list[int]) -> int:
        chk = 1
        for value in values:
            top = chk >> 25
            chk = (chk & 0x1FFFFFF) << 5 ^ value
            for i, g in enumerate(_GENERATOR):
                chk ^= g if (top >> i) & 1 else 0
        return chk

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


Bech32Key.PUBLIC = Bech32Key("npub")
Bech32Key.SECRET = Bech32Key("nsec")
