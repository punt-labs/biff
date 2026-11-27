"""Tests for NIP-19 bech32 key encoding."""

from __future__ import annotations

import pytest

from biff.nostr_bech32 import Bech32Key

_PUBLIC = bytes.fromhex(
    "3bf0c63fcb93463407af97a5e5ee64fa883d107ef9e558472c4eb9aaaefa459d"
)
_NPUB = "npub180cvv07tjdrrgpa0j7j7tmnyl2yr6yr7l8j4s3evf6u64th6gkwsyjh6w6"
_SECRET = bytes.fromhex(
    "67dea2ed018072d675f5415ecfaed7d2597555e202d85b3d65ea4e58d2d92ffa"
)
_NSEC = "nsec1vl029mgpspedva04g90vltkh6fvh240zqtv9k0t9af8935ke9laqsnlfe5"


def test_npub_encoding_matches_nip19() -> None:
    assert Bech32Key.PUBLIC.encode(_PUBLIC) == _NPUB


def test_npub_decoding_matches_nip19() -> None:
    assert Bech32Key.PUBLIC.decode(_NPUB) == _PUBLIC


def test_nsec_round_trip_matches_nip19() -> None:
    assert Bech32Key.SECRET.encode(_SECRET) == _NSEC
    assert Bech32Key.SECRET.decode(_NSEC) == _SECRET


def test_decode_rejects_bad_checksum() -> None:
    with pytest.raises(ValueError, match="checksum"):
        Bech32Key.PUBLIC.decode(_NPUB[:-1] + "q")


def test_decode_npub_rejects_nsec_prefix() -> None:
    with pytest.raises(ValueError, match="npub"):
        Bech32Key.PUBLIC.decode(_NSEC)


def test_decode_accepts_an_all_uppercase_npub() -> None:
    assert Bech32Key.PUBLIC.decode(_NPUB.upper()) == _PUBLIC


def test_decode_rejects_a_mixed_case_npub() -> None:
    mixed = _NPUB[:10] + _NPUB[10:].upper()
    with pytest.raises(ValueError, match="mixed case"):
        Bech32Key.PUBLIC.decode(mixed)


def test_decode_rejects_a_non_ascii_lookalike() -> None:
    # KELVIN SIGN lowercases to an ASCII "k", which is in the bech32 alphabet.
    lookalike = _NPUB.replace("k", chr(0x212A), 1)
    assert lookalike.lower() == _NPUB
    with pytest.raises(ValueError, match="ASCII"):
        Bech32Key.PUBLIC.decode(lookalike)
