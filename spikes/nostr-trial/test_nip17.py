"""NIP-17 gift-wrapped DMs: nostr-sdk against an independent Python unwrap.

Buzz publishes no crypto fixtures for NIP-17. Its relay treats a gift wrap as
an opaque kind:1059 event signed by an ephemeral key, addressed by a `p` tag
(crates/buzz-test-client/tests/e2e_nostr_interop.rs). So interoperability is
checked in both directions against nip44_ref, which passes every official
NIP-44 vector (test_nip44.py): the wrapper is nostr-sdk and the unwrapper is
plain Python, and the other way round.
"""

from __future__ import annotations

import json
import os
import time

from coincurve import PrivateKey
from nostr_sdk import (
    Event,
    Keys,
    PublicKey,
    UnwrappedGift,
    nip17_make_private_msg,
)

import nip44_ref
import nip_oa

GIFT_WRAP_KIND = 1059
SEAL_KIND = 13
PRIVATE_MESSAGE_KIND = 14


def _signed(secret: bytes, fields: dict) -> dict:
    pubkey = PrivateKey(secret).public_key_xonly.format().hex()
    event = {**fields, "pubkey": pubkey}
    event["id"] = nip_oa.event_id(event)
    event["sig"] = PrivateKey(secret).sign_schnorr(bytes.fromhex(event["id"])).hex()
    return event


def _compact(value: dict) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def test_sdk_round_trip() -> None:
    sender, recipient = Keys.generate(), Keys.generate()
    wrap = nip17_make_private_msg(sender, recipient.public_key(), "hello from biff")

    wrap_json = json.loads(wrap.as_json())
    assert wrap_json["kind"] == GIFT_WRAP_KIND
    assert ["p", recipient.public_key().to_hex()] in wrap_json["tags"]
    assert wrap_json["pubkey"] != sender.public_key().to_hex()  # ephemeral author
    assert wrap.verify()

    gift = UnwrappedGift.from_gift_wrap(recipient, wrap)
    assert gift.sender().to_hex() == sender.public_key().to_hex()
    assert gift.rumor().content() == "hello from biff"
    assert json.loads(gift.rumor().as_json())["kind"] == PRIVATE_MESSAGE_KIND


def test_independent_python_unwrap_of_sdk_wrap() -> None:
    sender, recipient = Keys.generate(), Keys.generate()
    recipient_secret = bytes.fromhex(recipient.secret_key().to_hex())
    wrap = nip17_make_private_msg(sender, recipient.public_key(), "héllo 🍕")
    wrap_json = json.loads(wrap.as_json())
    nip_oa.verify_event(wrap_json)

    wrap_key = nip44_ref.conversation_key(
        recipient_secret, bytes.fromhex(wrap_json["pubkey"])
    )
    seal = json.loads(nip44_ref.decrypt(wrap_json["content"], wrap_key))
    assert seal["kind"] == SEAL_KIND
    assert seal["tags"] == []
    nip_oa.verify_event(seal)  # the seal is signed by the sender
    assert seal["pubkey"] == sender.public_key().to_hex()

    seal_key = nip44_ref.conversation_key(
        recipient_secret, bytes.fromhex(seal["pubkey"])
    )
    rumor = json.loads(nip44_ref.decrypt(seal["content"], seal_key))
    assert rumor["kind"] == PRIVATE_MESSAGE_KIND
    assert rumor["content"] == "héllo 🍕"
    assert rumor["pubkey"] == seal["pubkey"]
    assert "sig" not in rumor  # a rumor is unsigned: deniable
    assert nip_oa.event_id(rumor) == rumor["id"]


def test_sdk_unwraps_python_built_wrap() -> None:
    sender, recipient = Keys.generate(), Keys.generate()
    sender_secret = bytes.fromhex(sender.secret_key().to_hex())
    recipient_pub = recipient.public_key().to_hex()
    now = int(time.time())

    rumor_fields = {
        "created_at": now,
        "kind": PRIVATE_MESSAGE_KIND,
        "tags": [["p", recipient_pub]],
        "content": "built in Python",
    }
    rumor = {
        **rumor_fields,
        "pubkey": sender.public_key().to_hex(),
    }
    rumor["id"] = nip_oa.event_id(rumor)

    seal_key = nip44_ref.conversation_key(sender_secret, bytes.fromhex(recipient_pub))
    seal = _signed(
        sender_secret,
        {
            "created_at": now,
            "kind": SEAL_KIND,
            "tags": [],
            "content": nip44_ref.encrypt(_compact(rumor), seal_key, os.urandom(32)),
        },
    )

    ephemeral = os.urandom(32)
    wrap_key = nip44_ref.conversation_key(ephemeral, bytes.fromhex(recipient_pub))
    wrap = _signed(
        ephemeral,
        {
            "created_at": now,
            "kind": GIFT_WRAP_KIND,
            "tags": [["p", recipient_pub]],
            "content": nip44_ref.encrypt(_compact(seal), wrap_key, os.urandom(32)),
        },
    )

    event = Event.from_json(_compact(wrap))
    assert event.verify()
    gift = UnwrappedGift.from_gift_wrap(recipient, event)
    assert gift.sender().to_hex() == sender.public_key().to_hex()
    assert gift.rumor().content() == "built in Python"
    assert PublicKey.parse(recipient_pub) == recipient.public_key()
