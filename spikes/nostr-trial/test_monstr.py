"""Runner-up candidate: does monstr 0.1.9 pass the official NIP-44 vectors?

monstr is pure Python (pycryptodome + secp256k1) and ships NIP-44 v2 and a
NIP-59 GiftWrap class. Run with the `candidates` extra installed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from monstr.encrypt import Keys, NIP44Encrypt

VECTORS = json.loads((Path(__file__).parent / "nip44.vectors.json").read_text())["v2"]


@pytest.mark.parametrize("vector", VECTORS["valid"]["encrypt_decrypt"])
def test_monstr_decrypts_official_payloads(vector: dict) -> None:
    recipient = NIP44Encrypt(Keys(priv_k=vector["sec2"]))
    sender_pub = Keys(priv_k=vector["sec1"]).public_key_hex()
    assert recipient.decrypt(vector["payload"], sender_pub) == vector["plaintext"]


@pytest.mark.parametrize("vector", VECTORS["valid"]["encrypt_decrypt"])
def test_monstr_round_trip(vector: dict) -> None:
    sender = NIP44Encrypt(Keys(priv_k=vector["sec1"]))
    recipient_pub = Keys(priv_k=vector["sec2"]).public_key_hex()
    payload = sender.encrypt(vector["plaintext"], recipient_pub)
    receiver = NIP44Encrypt(Keys(priv_k=vector["sec2"]))
    sender_pub = Keys(priv_k=vector["sec1"]).public_key_hex()
    assert receiver.decrypt(payload, sender_pub) == vector["plaintext"]
