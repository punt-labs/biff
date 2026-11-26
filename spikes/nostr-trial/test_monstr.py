"""Runner-up candidate: does monstr 0.1.9 pass the official NIP-44 vectors?

monstr is pure Python (pycryptodome + secp256k1) and ships NIP-44 v2 and a
NIP-59 GiftWrap class. Run with the `candidates` extra installed. The vectors
come from conftest.py.
"""

from __future__ import annotations

from monstr.encrypt import Keys, NIP44Encrypt


def test_monstr_decrypts_official_payloads(encrypt_decrypt_vector: dict) -> None:
    vector = encrypt_decrypt_vector
    recipient = NIP44Encrypt(Keys(priv_k=vector["sec2"]))
    sender_pub = Keys(priv_k=vector["sec1"]).public_key_hex()
    assert recipient.decrypt(vector["payload"], sender_pub) == vector["plaintext"]


def test_monstr_round_trip(encrypt_decrypt_vector: dict) -> None:
    vector = encrypt_decrypt_vector
    sender = NIP44Encrypt(Keys(priv_k=vector["sec1"]))
    recipient_pub = Keys(priv_k=vector["sec2"]).public_key_hex()
    payload = sender.encrypt(vector["plaintext"], recipient_pub)
    receiver = NIP44Encrypt(Keys(priv_k=vector["sec2"]))
    sender_pub = Keys(priv_k=vector["sec1"]).public_key_hex()
    assert receiver.decrypt(payload, sender_pub) == vector["plaintext"]
